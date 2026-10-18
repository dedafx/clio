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
#include <pxr/usd/ar/timestamp.h>
#include <pxr/usd/ar/writableAsset.h>

#include <functional>

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

// Wraps the default writable asset so that a new file is added to Perforce
// after it has been written and closed.
class ClioWritableAsset final : public ArWritableAsset {
public:
    ClioWritableAsset(std::shared_ptr<ArWritableAsset> inner, std::function<void()> onClosed)
        : _inner(std::move(inner)), _onClosed(std::move(onClosed)) {}

    ~ClioWritableAsset() override { Close(); }

    bool Close() override {
        const bool ok = _inner->Close();
        if (!_closed) {
            _closed = true;
            if (ok && _onClosed) {
                try {
                    _onClosed();
                } catch (...) {
                    // Adding to Perforce is best effort; the file is saved.
                }
            }
        }
        return ok;
    }

    size_t Write(const void* buffer, size_t count, size_t offset) override {
        return _inner->Write(buffer, count, offset);
    }

private:
    std::shared_ptr<ArWritableAsset> _inner;
    std::function<void()> _onClosed;
    bool _closed = false;
};

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

ArTimestamp ClioResolver::_GetModificationTimestamp(const std::string& assetPath,
                                                    const ArResolvedPath& resolvedPath) const {
    // Reload() compares timestamps to decide whether a layer changed. The
    // file's own time is right unless the client uses `modtime`, where two
    // revisions can share a time; then the workspace revision tells them apart.
    try {
        if (const auto core = _GetCoreResolver()) {
            if (const auto revision = core->revisionTimestamp(resolvedPath.GetPathString())) {
                return ArTimestamp(*revision);
            }
        }
    } catch (const std::exception&) {
    }
    return ArDefaultResolver::_GetModificationTimestamp(assetPath, resolvedPath);
}

ArResolvedPath ClioResolver::_ResolveForNewAsset(const std::string& assetPath) const {
    try {
        if (const auto core = _GetCoreResolver()) {
            const std::filesystem::path projectPath = _ProjectPath(*core, assetPath);
            if (!projectPath.empty()) {
                // A new file inside the project goes into the workspace.
                return ArResolvedPath(projectPath.lexically_normal().generic_string());
            }
        }
    } catch (const std::exception&) {
    }
    return ArDefaultResolver::_ResolveForNewAsset(assetPath);
}

bool ClioResolver::_PrepareForWrite(const ArResolvedPath& resolvedPath, bool* isNew,
                                    std::string* whyNot) const {
    const std::string path = resolvedPath.GetPathString();
    try {
        const auto core = _GetCoreResolver();
        if (!core) {
            return true; // no Clio context: behave like the default resolver
        }
        const auto match = core->workspace().matchLocalPath(path);
        if (!match) {
            return true; // not a project file
        }
        if (match->inVersionStore) {
            if (whyNot) {
                *whyNot = path + " is a stored version (" + (match->pin ? match->pin->str() : "") +
                          "), which is read-only. Save to the workspace instead.";
            }
            return false;
        }
        const clio::core::WriteCheck check = core->prepareForWrite(match->id);
        if (!check.allowed) {
            if (whyNot) {
                *whyNot = check.whyNot;
            }
            return false;
        }
        if (!check.warning.empty()) {
            _WarnOnce(path, check.warning);
        }
        *isNew = check.isNew;
    } catch (const std::exception& e) {
        // Clio is an enhancement, not a barrier: let the save go ahead.
        _WarnOnce(path, std::string("could not open for edit in Perforce: ") + e.what());
    } catch (...) {
        _WarnOnce(path, "could not open for edit in Perforce (unknown error)");
    }
    return true;
}

bool ClioResolver::_CanWriteAssetToPath(const ArResolvedPath& resolvedPath, std::string* whyNot) const {
    if (!ArDefaultResolver::_CanWriteAssetToPath(resolvedPath, whyNot)) {
        return false;
    }
    try {
        const auto core = _GetCoreResolver();
        const auto match = core ? core->workspace().matchLocalPath(resolvedPath.GetPathString())
                                : std::nullopt;
        if (match && match->inVersionStore) {
            if (whyNot) {
                *whyNot = "a stored version (" + (match->pin ? match->pin->str() : std::string()) +
                          ") is read-only; save to the workspace instead";
            }
            return false;
        }
    } catch (const std::exception&) {
    }
    return true;
}

std::shared_ptr<ArWritableAsset> ClioResolver::_OpenAssetForWrite(const ArResolvedPath& resolvedPath,
                                                                  WriteMode writeMode) const {
    // The file must be writable (opened for edit) before it is opened, and a
    // refusal must come before anything is written.
    bool isNew = false;
    std::string whyNot;
    if (!_PrepareForWrite(resolvedPath, &isNew, &whyNot)) {
        TF_RUNTIME_ERROR("clio: cannot write %s: %s", resolvedPath.GetPathString().c_str(), whyNot.c_str());
        return nullptr;
    }
    auto inner = ArDefaultResolver::_OpenAssetForWrite(resolvedPath, writeMode);
    if (!inner) {
        return nullptr;
    }

    if (!isNew) {
        return inner;
    }
    const std::string path = resolvedPath.GetPathString();
    // The context is bound to the calling thread; the asset may be closed
    // elsewhere, so keep the core resolver now.
    const auto core = _GetCoreResolver();
    if (!core) {
        return inner;
    }
    return std::make_shared<ClioWritableAsset>(std::move(inner), [this, core, path] {
        if (const auto match = core->workspace().matchLocalPath(path)) {
            const std::string warning = core->finishWrite(match->id);
            if (!warning.empty()) {
                _WarnOnce(path, warning);
            }
        }
    });
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

bool ClioResolver::_IsContextDependentPath(const std::string& assetPath) const {
    if (ArDefaultResolver::_IsContextDependentPath(assetPath)) {
        return true;
    }
    // A project path resolves to a different file for each pin (workspace or
    // version store). Telling Sdf so makes it look layers up by resolved
    // path, so a stage opened at one pin never reuses a layer opened at
    // another pin in the same process.
    try {
        if (const auto core = _GetCoreResolver()) {
            return !_ProjectPath(*core, assetPath).empty();
        }
    } catch (const std::exception&) {
    }
    return false;
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
