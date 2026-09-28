#pragma once

#include "clio/core/identifier.hpp"
#include "clio/core/p4/connection.hpp"
#include "clio/core/settings.hpp"

#include <filesystem>
#include <mutex>
#include <string>
#include <vector>

namespace clio::core {

/// A local path that belongs to the project: a file in the workspace, or a
/// version in the version store.
struct LocalPathMatch {
    AssetIdentifier id;
    std::optional<Pin> pin; ///< Set for version-store paths (the version the folder holds).
    bool inVersionStore = false;
};

/// Maps clio: identifiers to depot and local paths, and runs the Perforce
/// operations that put files on disk.
///
/// Thread-safe. For now all server work is serialized on one connection;
/// the connection pool and the miss coalescer (design doc §10.3) replace
/// this later without changing the interface.
class Workspace {
public:
    explicit Workspace(Settings settings);

    const Settings& settings() const { return _settings; }

    /// Depot path, for example //imagine/main/props/crate/crate.usd
    std::string depotPath(const AssetIdentifier& id) const;

    /// Path of the file inside the client workspace (latest and have pins).
    std::filesystem::path workspacePath(const AssetIdentifier& id) const;

    /// Path of a historical version in the read-only version store
    /// (change, label and revision pins, design doc §10.4).
    std::filesystem::path versionStorePath(const AssetIdentifier& id, const Pin& pin) const;

    /// Which project file a local path refers to, or nullopt if it is outside
    /// both the workspace root and this project's part of the version store.
    /// Purely lexical: no filesystem or server access.
    std::optional<LocalPathMatch> matchLocalPath(const std::filesystem::path& path) const;

    /// `p4 sync` the files to `pin` (latest, have or a snapshot) in the
    /// workspace. Files that do not exist in the depot are skipped. Files the
    /// user has opened are never overwritten (Perforce refuses to).
    p4::CommandResult sync(const std::vector<AssetIdentifier>& ids, const Pin& pin);

    /// `p4 print` one historical version into the version store. Writes to a
    /// temporary file first and renames it, so readers never see a partial
    /// file. Returns false if the file does not exist at that version.
    bool fetchVersion(const AssetIdentifier& id, const Pin& pin);

    /// Run any command on this workspace's connection (serialized).
    p4::CommandResult run(const std::string& command,
                          const std::vector<std::string>& args = {},
                          const std::optional<std::string>& input = std::nullopt);

private:
    Settings _settings;
    std::mutex _mutex;
    p4::Connection _connection;
};

} // namespace clio::core
