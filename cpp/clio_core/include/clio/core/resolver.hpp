#pragma once

#include "clio/core/identifier.hpp"
#include "clio/core/settings.hpp"
#include "clio/core/workspace.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace clio::core {

/// The result of resolving a clio: identifier.
struct ResolvedAsset {
    AssetIdentifier id;
    Pin pin;                          ///< The pin that was applied.
    std::string depotPath;            ///< For example //imagine/main/props/crate/crate.usd
    std::filesystem::path localPath;  ///< The file on disk.

    /// Set when Clio could not deliver exactly what was asked and used a
    /// local file instead, for example because the server was unreachable.
    /// Callers should show it to the user. Empty otherwise.
    std::string warning;
};

/// Resolution rules shared by the USD plugin and Python (design doc §10.4),
/// with no USD or Python dependency.
///
///   latest   sync into the workspace (policy sync), then use the workspace file
///   have     use the workspace file; never contacts the server
///   @change, #rev
///            use the version store; fetch with p4 print if missing
///   @label   use the version store, but fetch again once per process,
///            because labels can be moved
///
/// The server is the authority (design §8.3): results are remembered in
/// memory only, until refresh(). Thread-safe.
///
/// Clio is an enhancement, not a barrier: with the sync and offline
/// policies, if the server cannot be reached (or refuses, for example
/// because of an expired login), the local file is used with a warning.
/// After a server failure Clio stops contacting the server for
/// `serverRetryInterval` so that later resolves do not wait again. Only
/// the verify policy fails instead of falling back.
class AssetResolver {
public:
    explicit AssetResolver(Settings settings);

    const Settings& settings() const { return _workspace.settings(); }
    Workspace& workspace() { return _workspace; }

    /// The pin that applies to `id`: its own pin, else the settings pin.
    Pin effectivePin(const AssetIdentifier& id) const;

    /// Resolve to a local file, fetching it if the policy allows. Returns
    /// nullopt if the asset does not exist (or is not present, for the
    /// verify and offline policies). Throws Error on server or config errors.
    std::optional<ResolvedAsset> resolve(const AssetIdentifier& id);

    /// Where a new file for `id` would be written in the workspace. Makes no
    /// server calls.
    std::filesystem::path resolveForNewAsset(const AssetIdentifier& id) const;

    /// Forget remembered results, so the next resolve checks again. Also
    /// retries a server that was unavailable.
    void refresh();

    /// How long to stop contacting the server after it failed.
    static constexpr std::chrono::seconds serverRetryInterval{60};

private:
    std::optional<ResolvedAsset> _resolveUncached(const AssetIdentifier& id, const Pin& pin);
    std::optional<ResolvedAsset> _useWorkspaceFile(ResolvedAsset asset, const std::string& reason) const;
    bool _serverAvailable() const;
    void _markServerUnavailable();

    Workspace _workspace;
    std::atomic<std::int64_t> _serverRetryAt{0}; ///< steady_clock ticks; 0 = available
    std::mutex _memoMutex;
    std::unordered_map<std::string, ResolvedAsset> _memo;
};

} // namespace clio::core
