#include "resolver.h"

#include "resolverContext.h"

#include "clio/core/error.hpp"
#include "clio/core/identifier.hpp"
#include "clio/core/resolver.hpp"
#include "clio/core/settings.hpp"

#include <pxr/base/tf/diagnostic.h>
#include <pxr/base/tf/getenv.h>
#include <pxr/base/vt/dictionary.h>
#include <pxr/base/vt/value.h>
#include <pxr/usd/ar/assetInfo.h>
#include <pxr/usd/ar/defineResolver.h>
#include <pxr/usd/ar/filesystemAsset.h>
#include <pxr/usd/ar/filesystemWritableAsset.h>
#include <pxr/usd/ar/notice.h>

#include <atomic>

using clio::core::AssetIdentifier;

PXR_NAMESPACE_OPEN_SCOPE

AR_DEFINE_RESOLVER(ClioResolver, ArResolver);

namespace {

constexpr const char* contextEnvVar = "CLIO_RESOLVER_CONTEXT";

// Exceptions must never escape into USD, which calls the resolver from its
// worker threads. Report and fail the single call instead.
template <class Fn, class Result>
Result guarded(const char* what, const std::string& assetPath, Result failed, Fn&& fn) {
    try {
        return fn();
    } catch (const std::exception& e) {
        TF_WARN("clio: %s '%s' failed: %s", what, assetPath.c_str(), e.what());
    } catch (...) {
        TF_WARN("clio: %s '%s' failed with an unknown error", what, assetPath.c_str());
    }
    return failed;
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
    const ArResolverContext fallback = _CreateDefaultContext();
    if (const auto* ctx = fallback.Get<ClioResolverContext>()) {
        return _GetCoreResolver(*ctx);
    }

    static std::atomic<bool> warned{false};
    if (!warned.exchange(true)) {
        TF_WARN("clio: no resolver context. Open the stage with a clio context "
                "(Ar.GetResolver().CreateContextFromString(\"clio\", ...)) or set $%s.",
                contextEnvVar);
    }
    return nullptr;
}

std::string ClioResolver::_CreateIdentifier(const std::string& assetPath,
                                            const ArResolvedPath& anchorAssetPath) const {
    return guarded("creating an identifier for", assetPath, std::string(), [&] {
        if (AssetIdentifier::isClioUri(assetPath)) {
            return AssetIdentifier::parse(assetPath).str();
        }
        const std::string& anchor = anchorAssetPath.GetPathString();
        if (AssetIdentifier::isClioUri(anchor)) {
            return AssetIdentifier::anchor(assetPath, AssetIdentifier::parse(anchor)).str();
        }
        // Not a clio path; Ar only calls this resolver for the clio: scheme.
        return assetPath;
    });
}

std::string ClioResolver::_CreateIdentifierForNewAsset(const std::string& assetPath,
                                                       const ArResolvedPath& anchorAssetPath) const {
    return _CreateIdentifier(assetPath, anchorAssetPath);
}

ArResolvedPath ClioResolver::_Resolve(const std::string& assetPath) const {
    return guarded("resolving", assetPath, ArResolvedPath(), [&] {
        const auto core = _GetCoreResolver();
        if (!core) {
            return ArResolvedPath();
        }
        const auto asset = core->resolve(AssetIdentifier::parse(assetPath));
        return asset ? ArResolvedPath(asset->localPath.generic_string()) : ArResolvedPath();
    });
}

ArResolvedPath ClioResolver::_ResolveForNewAsset(const std::string& assetPath) const {
    return guarded("resolving new asset", assetPath, ArResolvedPath(), [&] {
        const auto core = _GetCoreResolver();
        if (!core) {
            return ArResolvedPath();
        }
        // TODO(design §10.6): open for add/edit (and lock) when saving.
        return ArResolvedPath(
            core->resolveForNewAsset(AssetIdentifier::parse(assetPath)).generic_string());
    });
}

ArResolverContext ClioResolver::_CreateDefaultContext() const {
    // Read once per process, like USD's own resolver settings, so an invalid
    // value is reported once rather than on every resolve.
    static const ArResolverContext defaultContext = [] {
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
    return defaultContext;
}

ArResolverContext ClioResolver::_CreateDefaultContextForAsset(const std::string& /*assetPath*/) const {
    return _CreateDefaultContext();
}

ArResolverContext ClioResolver::_CreateContextFromString(const std::string& contextStr) const {
    std::string error;
    ClioResolverContext ctx(contextStr, &error);
    if (ctx.IsEmpty()) {
        TF_CODING_ERROR("clio: invalid resolver context '%s': %s", contextStr.c_str(), error.c_str());
        return ArResolverContext();
    }
    return ArResolverContext(ctx);
}

bool ClioResolver::_IsContextDependentPath(const std::string& assetPath) const {
    // The pin (and the project) come from the bound context.
    return AssetIdentifier::isClioUri(assetPath);
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
    }
    ArNotice::ResolverChanged(*ctx).Send();
}

ArAssetInfo ClioResolver::_GetAssetInfo(const std::string& assetPath,
                                        const ArResolvedPath& /*resolvedPath*/) const {
    return guarded("getting asset info for", assetPath, ArAssetInfo(), [&] {
        ArAssetInfo info;
        const auto core = _GetCoreResolver();
        if (!core || !AssetIdentifier::isClioUri(assetPath)) {
            return info;
        }
        // No server calls: everything here is known from the identifier and
        // the context.
        const auto id = AssetIdentifier::parse(assetPath);
        const auto pin = core->effectivePin(id);
        info.version = pin.str();
        info.assetName = id.path();
        VtDictionary resolverInfo;
        resolverInfo["depotPath"] = VtValue(core->workspace().depotPath(id));
        resolverInfo["pin"] = VtValue(pin.str());
        info.resolverInfo = VtValue(resolverInfo);
        return info;
    });
}

ArTimestamp ClioResolver::_GetModificationTimestamp(const std::string& /*assetPath*/,
                                                    const ArResolvedPath& resolvedPath) const {
    // TODO(design §10.3): derive from the revision/change number so Reload()
    // follows server versions rather than file times.
    return ArFilesystemAsset::GetModificationTimestamp(resolvedPath);
}

std::shared_ptr<ArAsset> ClioResolver::_OpenAsset(const ArResolvedPath& resolvedPath) const {
    return ArFilesystemAsset::Open(resolvedPath);
}

std::shared_ptr<ArWritableAsset> ClioResolver::_OpenAssetForWrite(const ArResolvedPath& resolvedPath,
                                                                  WriteMode writeMode) const {
    // TODO(design §10.6): check locks and open for edit before writing.
    return ArFilesystemWritableAsset::Create(resolvedPath, writeMode);
}

PXR_NAMESPACE_CLOSE_SCOPE
