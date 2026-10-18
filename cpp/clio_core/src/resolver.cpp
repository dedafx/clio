#include "clio/core/resolver.hpp"

#include "clio/core/error.hpp"

namespace clio::core {

AssetResolver::AssetResolver(Settings settings, Clock clock)
    : _clock(std::move(clock)),
      _workspace(std::move(settings)),
      _syncBatcher(
          [this](const std::vector<AssetIdentifier>& ids, const Pin& pin, bool force) {
              _workspace.sync(ids, pin, force);
          },
          _workspace.settings().batchWindow) {}

std::chrono::steady_clock::time_point AssetResolver::_now() const {
    return _clock ? _clock() : std::chrono::steady_clock::now();
}

Pin AssetResolver::effectivePin(const AssetIdentifier& id) const {
    return id.pin() ? *id.pin() : settings().pin;
}

std::optional<ResolvedAsset> AssetResolver::resolve(const AssetIdentifier& id) {
    const Pin pin = effectivePin(id);
    const std::string key = id.path() + "|" + pin.str();

    {
        std::lock_guard<std::mutex> lock(_memoMutex);
        if (auto it = _memo.find(key); it != _memo.end()) {
            if (it->second.forever || _now() < it->second.expires) {
                return it->second.asset;
            }
            _memo.erase(it); // too old: ask again
        }
    }

    // Threads that miss on the same asset share one resolve.
    return _resolves.run(key, [&] { return _resolveAndRemember(id, pin, key); });
}

std::optional<ResolvedAsset> AssetResolver::_resolveAndRemember(const AssetIdentifier& id, const Pin& pin,
                                                                const std::string& key) {
    bool serverAnswered = false;
    std::optional<ResolvedAsset> resolved = _resolveUncached(id, pin, serverAnswered);
    // A fallback answer is not remembered, so the exact version is fetched
    // once the server is reachable again. "Not found" is remembered only
    // when the server said so.
    const bool remember = resolved ? resolved->warning.empty() : serverAnswered;
    if (!remember) {
        return resolved;
    }
    // A change or revision names content that never changes (design §8.3
    // rule 2). Everything else can move and is only trusted for a while.
    const bool forever = pin.kind() == Pin::Kind::Change || pin.kind() == Pin::Kind::Revision;
    const auto ttl = settings().cacheTtl;
    if (forever || ttl.count() > 0) {
        std::lock_guard<std::mutex> lock(_memoMutex);
        _memo[key] = MemoEntry{resolved, forever, _now() + ttl};
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
    return retryAt == 0 || _now().time_since_epoch().count() >= retryAt;
}

void AssetResolver::_markServerUnavailable() {
    const auto retryAt = _now() + serverRetryInterval;
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
            // Different files fetch in parallel on pooled connections; the
            // same file is never fetched twice at once (single-flight).
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
                _syncBatcher.sync(id, Pin::latest());
                if (!std::filesystem::exists(asset.localPath)) {
                    // Perforce thinks it is here, but it was deleted from
                    // disk: restore the revision the workspace has.
                    _syncBatcher.sync(id, Pin::have(), /*force=*/true);
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
            // A no-op on the server when the file is already current; the
            // memo skips the call for `cacheTtl` after an answer.
            _syncBatcher.sync(id, pin);
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

WriteCheck AssetResolver::prepareForWrite(const AssetIdentifier& id) {
    if (settings().policy != Policy::Sync) {
        return {};
    }
    const std::string notSaved = "; the file is saved locally and not opened for edit in Perforce";
    if (!_serverAvailable()) {
        WriteCheck check;
        check.warning = "Perforce is not available" + notSaved;
        return check;
    }
    try {
        return _workspace.prepareForWrite(id);
    } catch (const P4Error& e) {
        _markServerUnavailable();
        WriteCheck check;
        check.warning = std::string("Perforce is not available (") + e.what() + ")" + notSaved;
        return check;
    }
}

std::string AssetResolver::finishWrite(const AssetIdentifier& id) {
    if (settings().policy != Policy::Sync || !_serverAvailable()) {
        return {};
    }
    try {
        return _workspace.addNewFile(id);
    } catch (const P4Error& e) {
        _markServerUnavailable();
        return id.path() + " was saved but not added to Perforce (" + e.what() + ")";
    }
}

void AssetResolver::refresh() {
    std::lock_guard<std::mutex> lock(_memoMutex);
    _memo.clear();
    _serverRetryAt.store(0);
}

} // namespace clio::core
