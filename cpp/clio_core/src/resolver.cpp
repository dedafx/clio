#include "clio/core/resolver.hpp"

#include "clio/core/error.hpp"

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
    bool serverAnswered = false;
    std::optional<ResolvedAsset> resolved = _resolveUncached(id, pin, serverAnswered);
    // A fallback answer is not remembered, so the exact version is fetched
    // once the server is reachable again. "Not found" is remembered only
    // when the server said so.
    const bool remember = resolved ? resolved->warning.empty() : serverAnswered;
    if (remember) {
        std::lock_guard<std::mutex> lock(_memoMutex);
        _memo.emplace(key, resolved);
    }
    return resolved;
}

bool AssetResolver::manages(const std::filesystem::path& localPath) const {
    return _workspace.matchLocalPath(localPath).has_value();
}

std::optional<ResolvedAsset> AssetResolver::resolvePath(const std::filesystem::path& localPath) {
    const auto match = _workspace.matchLocalPath(localPath);
    if (!match) {
        return std::nullopt;
    }
    return resolve(match->id);
}

bool AssetResolver::_serverAvailable() const {
    const auto retryAt = _serverRetryAt.load();
    return retryAt == 0 || std::chrono::steady_clock::now().time_since_epoch().count() >= retryAt;
}

void AssetResolver::_markServerUnavailable() {
    const auto retryAt = std::chrono::steady_clock::now() + serverRetryInterval;
    _serverRetryAt.store(retryAt.time_since_epoch().count());
}

std::optional<ResolvedAsset> AssetResolver::_useWorkspaceFile(ResolvedAsset asset,
                                                              const std::string& reason) const {
    asset.localPath = _workspace.workspacePath(asset.id);
    if (!std::filesystem::exists(asset.localPath)) {
        return std::nullopt;
    }
    asset.warning = reason + "; using the local file " + asset.localPath.string() +
                    ", which may not be version " + asset.pin.str();
    return asset;
}

std::optional<ResolvedAsset> AssetResolver::_resolveUncached(const AssetIdentifier& id,
                                                             const Pin& pin, bool& serverAnswered) {
    const Policy policy = settings().policy;
    ResolvedAsset asset{id, pin, _workspace.depotPath(id), {}, {}};
    const bool mayContactServer = policy == Policy::Sync && _serverAvailable();
    const std::string unavailable = "Perforce is not available";

    if (pin.isHistorical()) {
        asset.localPath = _workspace.versionStorePath(id, pin);
        const bool stored = std::filesystem::exists(asset.localPath);
        if (policy == Policy::Verify) {
            // Strict: the exact version or nothing.
            return stored ? std::optional<ResolvedAsset>(asset) : std::nullopt;
        }
        // Change and revision pins name content that never changes, so a
        // stored file is reused. A label can be moved by an admin, so it is
        // fetched from the server again once per process (design §8.3).
        if (stored && pin.kind() != Pin::Kind::Label) {
            return asset;
        }
        if (stored && !mayContactServer) {
            if (policy == Policy::Sync) {
                // The server is down (or in its retry interval): the label
                // may have moved. The warning also keeps this answer out of
                // the memo, so the label is fetched once the server is back.
                asset.warning = unavailable + "; using the stored copy of label " + pin.labelName() +
                                ", which may be out of date";
            }
            return asset;
        }
        if (!mayContactServer) {
            return _useWorkspaceFile(asset, policy == Policy::Offline ? "Offline" : unavailable);
        }
        try {
            const FetchResult result = _workspace.fetchVersion(id, pin);
            serverAnswered = true;
            if (result == FetchResult::NotFound) {
                return std::nullopt; // the file does not exist at that version
            }
            if (result == FetchResult::KeptExisting && pin.kind() == Pin::Kind::Label) {
                asset.warning = "Could not replace the stored copy of label " + pin.labelName() +
                                " (it may be in use); it may be out of date";
            }
            return asset;
        } catch (const P4Error& e) {
            _markServerUnavailable();
            if (stored) {
                asset.warning = unavailable + " (" + e.what() + "); using the stored copy of label " +
                                pin.labelName() + ", which may be out of date";
                return asset;
            }
            return _useWorkspaceFile(asset, unavailable + " (" + e.what() + ")");
        }
    }

    asset.localPath = _workspace.workspacePath(id);

    if (pin.kind() == Pin::Kind::Have) {
        // The file on disk is the version to use, whatever its revision.
        if (std::filesystem::exists(asset.localPath)) {
            return asset;
        }
        // Not on disk: fetch it, if the policy allows the server.
        if (mayContactServer) {
            try {
                // Never synced to this workspace: take the latest revision.
                _workspace.sync({id}, Pin::latest());
                if (!std::filesystem::exists(asset.localPath)) {
                    // Perforce thinks it is here, but it was deleted from
                    // disk: restore the revision the workspace has.
                    _workspace.sync({id}, Pin::have(), /*force=*/true);
                }
                serverAnswered = true;
            } catch (const P4Error&) {
                _markServerUnavailable();
            }
        }
        if (!std::filesystem::exists(asset.localPath)) {
            return std::nullopt;
        }
        return asset;
    }

    if (pin.kind() == Pin::Kind::Latest && mayContactServer) {
        try {
            // A no-op on the server when the file is already current.
            // TODO(design §10.3): skip the call when the file is known current.
            _workspace.sync({id}, pin);
            serverAnswered = true;
        } catch (const P4Error& e) {
            _markServerUnavailable();
            if (policy != Policy::Verify) {
                return _useWorkspaceFile(asset, unavailable + " (" + e.what() + ")");
            }
        }
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
    _serverRetryAt.store(0);
}

} // namespace clio::core
