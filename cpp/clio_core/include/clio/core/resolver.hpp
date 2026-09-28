#pragma once

#include "clio/core/identifier.hpp"
#include "clio/core/settings.hpp"
#include "clio/core/workspace.hpp"

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

    /// Forget remembered results, so the next resolve checks again.
    void refresh();

private:
    std::optional<ResolvedAsset> _resolveUncached(const AssetIdentifier& id, const Pin& pin);

    Workspace _workspace;
    std::mutex _memoMutex;
    std::unordered_map<std::string, ResolvedAsset> _memo;
};

} // namespace clio::core
