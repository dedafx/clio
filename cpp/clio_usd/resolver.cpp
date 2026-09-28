#include "resolver.h"

#include "resolverContext.h"

#include "clio/core/error.hpp"
#include "clio/core/resolver.hpp"
#include "clio/core/settings.hpp"

#include <pxr/base/tf/diagnostic.h>
#include <pxr/base/tf/getenv.h>
#include <pxr/base/vt/dictionary.h>
#include <pxr/base/vt/value.h>
#include <pxr/usd/ar/assetInfo.h>
#include <pxr/usd/ar/defineResolver.h>
#include <pxr/usd/ar/notice.h>

PXR_NAMESPACE_OPEN_SCOPE

AR_DEFINE_RESOLVER(ClioResolver, ArDefaultResolver);

namespace {

constexpr const char* contextEnvVar = "CLIO_RESOLVER_CONTEXT";

// The Clio context from $CLIO_RESOLVER_CONTEXT, read once per process like
// USD's own resolver settings, so an invalid value is reported only once.
const ArResolverContext& environmentContext() {
    static const ArResolverContext context = [] {
        const std::string settings = TfGetenv(contextEnvVar);
        if (settings.empty()) {
            return ArResolverContext();
        }
        std::string error;
        ClioResolverContext ctx(settings, &error);
        if (ctx.IsEmpty()) {
            TF_WARN("clio: $%s is invalid: %s", contextEnvVar, error.c_str());
            return ArResolverContext();
        }
        return ArResolverContext(ctx);
    }();
    return context;
}

ArResolverContext withEnvironmentContext(const ArResolverContext& base) {
    const ArResolverContext& clio = environmentContext();
    return clio.IsEmpty() ? base : ArResolverContext(std::vector<ArResolverContext>{base, clio});
}

// Paths that USD anchors to a layer ("./x", "../x") or that are absolute
// are handled as given. Other relative paths are search paths.
bool isSearchPath(const std::string& path) {
    if (path.empty() || std::filesystem::path(path).is_absolute()) {
        return false;
    }
    return path.rfind("./", 0) != 0 && path.rfind("../", 0) != 0;
}

} // namespace

ClioResolver::ClioResolver() = default;
ClioResolver::~ClioResolver() = default;

std::shared_ptr<clio::core::AssetResolver>
ClioResolver::_GetCoreResolver(const ClioResolverContext& ctx) const {
    if (ctx.IsEmpty()) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(_mutex);
    auto& slot = _coreResolvers[ctx.GetSettings()];
    if (!slot) {
        slot = std::make_shared<clio::core::AssetResolver>(
            clio::core::Settings::parse(ctx.GetSettings()));
    }
    return slot;
}

std::shared_ptr<clio::core::AssetResolver> ClioResolver::_GetCoreResolver() const {
    if (const auto* ctx = _GetCurrentContextObject<ClioResolverContext>()) {
        if (!ctx->IsEmpty()) {
            return _GetCoreResolver(*ctx);
        }
    }
    if (const auto* ctx = environmentContext().Get<ClioResolverContext>()) {
        return _GetCoreResolver(*ctx);
    }
    return nullptr; // no Clio context: plain ArDefaultResolver behaviour
}

std::filesystem::path ClioResolver::_ProjectPath(const clio::core::AssetResolver& core,
                                                 const std::string& assetPath) const {
    std::filesystem::path candidate;
    if (std::filesystem::path(assetPath).is_absolute()) {
        candidate = assetPath;
    } else if (isSearchPath(assetPath)) {
        // The workspace root acts as the first search path, so
        // "assets/crate/crate.usda" means <root>/assets/crate/crate.usda.
        candidate = core.settings().workspaceRoot / assetPath;
    } else {
        return {};
    }
    return core.manages(candidate) ? candidate : std::filesystem::path();
}

void ClioResolver::_WarnOnce(const std::string& assetPath, const std::string& warning) const {
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (!_warned.insert(assetPath).second) {
            return;
        }
    }
    TF_WARN("clio: %s: %s", assetPath.c_str(), warning.c_str());
}

ArResolvedPath ClioResolver::_Resolve(const std::string& assetPath) const {
    // Exceptions must never escape into USD, which calls this from worker
    // threads. Any problem falls back to the default resolver.
    try {
        if (const auto core = _GetCoreResolver()) {
            const std::filesystem::path projectPath = _ProjectPath(*core, assetPath);
            if (!projectPath.empty()) {
                if (const auto asset = core->resolvePath(projectPath)) {
                    if (!asset->warning.empty()) {
                        _WarnOnce(assetPath, asset->warning);
                    }
                    return ArResolvedPath(asset->localPath.generic_string());
                }
            }
        }
    } catch (const std::exception& e) {
        _WarnOnce(assetPath, std::string("could not use Perforce: ") + e.what());
    } catch (...) {
        _WarnOnce(assetPath, "could not use Perforce (unknown error)");
    }
    return ArDefaultResolver::_Resolve(assetPath);
}

ArResolverContext ClioResolver::_CreateDefaultContext() const {
    return withEnvironmentContext(ArDefaultResolver::_CreateDefaultContext());
}

ArResolverContext ClioResolver::_CreateDefaultContextForAsset(const std::string& assetPath) const {
    return withEnvironmentContext(ArDefaultResolver::_CreateDefaultContextForAsset(assetPath));
}

ArResolverContext ClioResolver::_CreateContextFromString(const std::string& contextStr) const {
    // Clio settings are key=value pairs. Anything else is the default
    // resolver's search-path string.
    if (contextStr.find('=') == std::string::npos) {
        return ArDefaultResolver::_CreateContextFromString(contextStr);
    }
    std::string error;
    ClioResolverContext ctx(contextStr, &error);
    if (ctx.IsEmpty()) {
        TF_CODING_ERROR("clio: invalid resolver context '%s': %s", contextStr.c_str(), error.c_str());
        return ArResolverContext();
    }
    return ArResolverContext(ctx);
}

void ClioResolver::_RefreshContext(const ArResolverContext& context) {
    const auto* ctx = context.Get<ClioResolverContext>();
    if (!ctx || ctx->IsEmpty()) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto it = _coreResolvers.find(ctx->GetSettings());
        if (it == _coreResolvers.end()) {
            return;
        }
        it->second->refresh();
        _warned.clear();
    }
    ArNotice::ResolverChanged(*ctx).Send();
}

ArAssetInfo ClioResolver::_GetAssetInfo(const std::string& assetPath,
                                        const ArResolvedPath& resolvedPath) const {
    ArAssetInfo info = ArDefaultResolver::_GetAssetInfo(assetPath, resolvedPath);
    try {
        const auto core = _GetCoreResolver();
        if (!core) {
            return info;
        }
        const auto match = core->workspace().matchLocalPath(resolvedPath.GetPathString());
        if (!match) {
            return info;
        }
        // No server calls: everything here is known from the path and context.
        const auto pin = match->pin ? *match->pin : core->effectivePin(match->id);
        info.version = pin.str();
        info.assetName = match->id.path();
        VtDictionary resolverInfo;
        resolverInfo["depotPath"] = VtValue(core->workspace().depotPath(match->id));
        resolverInfo["pin"] = VtValue(pin.str());
        info.resolverInfo = VtValue(resolverInfo);
    } catch (const std::exception&) {
        // Asset info is optional; return what the default resolver gave.
    }
    return info;
}

PXR_NAMESPACE_CLOSE_SCOPE
