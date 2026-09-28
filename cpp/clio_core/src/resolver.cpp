#include "clio/core/resolver.hpp"

namespace clio::core {

AssetResolver::AssetResolver(Settings settings) : _workspace(std::move(settings)) {}

Pin AssetResolver::effectivePin(const AssetIdentifier& id) const {
    return id.pin() ? *id.pin() : settings().pin;
}

std::optional<ResolvedAsset> AssetResolver::resolve(const AssetIdentifier& id) {
    const Pin pin = effectivePin(id);
    const std::string key = id.path() + "|" + pin.str();

    {
        std::lock_guard<std::mutex> lock(_memoMutex);
        if (auto it = _memo.find(key); it != _memo.end()) {
            return it->second;
        }
    }

    // TODO(design §10.3): route misses through the coalescer so concurrent
    // misses from USD worker threads become one batched sync.
    std::optional<ResolvedAsset> resolved = _resolveUncached(id, pin);
    if (resolved) {
        std::lock_guard<std::mutex> lock(_memoMutex);
        _memo.emplace(key, *resolved);
    }
    return resolved;
}

std::optional<ResolvedAsset> AssetResolver::_resolveUncached(const AssetIdentifier& id,
                                                             const Pin& pin) {
    const Policy policy = settings().policy;
    ResolvedAsset asset{id, pin, _workspace.depotPath(id), {}};

    if (pin.isHistorical()) {
        asset.localPath = _workspace.versionStorePath(id, pin);
        // Stored versions never change, so a stored file is always valid.
        // TODO(design §9.2): labels can be moved by admins; revalidate them.
        if (std::filesystem::exists(asset.localPath)) {
            return asset;
        }
        if (policy != Policy::Sync || !_workspace.fetchVersion(id, pin)) {
            return std::nullopt;
        }
        return asset;
    }

    asset.localPath = _workspace.workspacePath(id);
    if (pin.kind() == Pin::Kind::Latest && policy == Policy::Sync) {
        // A no-op on the server when the file is already current.
        // TODO(design §10.3): answer from the StatusCache manifest instead.
        _workspace.sync({id}, pin);
    }
    if (!std::filesystem::exists(asset.localPath)) {
        return std::nullopt;
    }
    return asset;
}

std::filesystem::path AssetResolver::resolveForNewAsset(const AssetIdentifier& id) const {
    return _workspace.workspacePath(id);
}

void AssetResolver::refresh() {
    std::lock_guard<std::mutex> lock(_memoMutex);
    _memo.clear();
}

} // namespace clio::core
