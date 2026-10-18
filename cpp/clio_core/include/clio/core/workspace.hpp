#pragma once

#include "clio/core/identifier.hpp"
#include "clio/core/p4/connection.hpp"
#include "clio/core/p4/pool.hpp"
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

/// Outcome of fetching a historical version into the version store.
enum class FetchResult {
    Fetched,      ///< Written (or replaced) from the server.
    NotFound,     ///< The file does not exist at that version.
    KeptExisting, ///< Fetched, but an existing stored copy could not be replaced and was kept.
};

/// What to do before a file in the workspace is written (design doc §10.6).
struct WriteCheck {
    bool allowed = true;
    /// Why the write is refused (for example "locked by sam"), when not allowed.
    std::string whyNot;
    /// Something the user should know although the write may go ahead, for
    /// example that the local copy is not the latest version.
    std::string warning;
    /// The file is not in Perforce. Call Workspace::addNewFile() after
    /// writing it.
    bool isNew = false;
};

/// Maps clio: identifiers to depot and local paths, and runs the Perforce
/// operations that put files on disk.
///
/// Thread-safe. Server work runs on a small pool of connections
/// (`Settings::maxConnections`), so threads fetching different files do not
/// wait for each other. Changes to the workspace itself (`sync`) are
/// serialized, in this process and across processes; reads into the version
/// store (`fetchVersion`) run in parallel.
class Workspace {
public:
    explicit Workspace(Settings settings);

    const Settings& settings() const { return _settings; }

    /// Depot path, for example //imagine/main/props/crate/crate.usd. The
    /// characters Perforce reserves (@ # % *) are escaped as %XX.
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
    /// user has opened are never overwritten (Perforce refuses to). `force`
    /// rewrites files Perforce believes are already there (`sync -f`), for
    /// example a file deleted from disk outside Perforce.
    p4::CommandResult sync(const std::vector<AssetIdentifier>& ids, const Pin& pin, bool force = false);

    /// `p4 print` one historical version into the version store. Writes to a
    /// temporary file first and renames it, so readers never see a partial
    /// file. Stored files are made read-only, so nothing edits a version in
    /// place.
    FetchResult fetchVersion(const AssetIdentifier& id, const Pin& pin);

    /// Make a workspace file ready to be written: open it for edit (which
    /// also locks it when its file type is exclusive, +l), or refuse before
    /// anything is lost if someone else holds the lock. A file that is not
    /// in Perforce is allowed and reported as `isNew`. Never submits.
    /// Two commands at most (`fstat`, `edit`), however often it is called
    /// for a file that is already open. Throws P4Error if the server cannot
    /// be reached.
    WriteCheck prepareForWrite(const AssetIdentifier& id);

    /// `p4 add` a file that was new when prepareForWrite() looked at it and
    /// has been written since. Returns a warning for the user, or an empty
    /// string if the file was opened for add (or was already open). A file
    /// that was not written is skipped. Throws P4Error if the server cannot
    /// be reached.
    std::string addNewFile(const AssetIdentifier& id);

    /// The version-store folder name for this server: the effective P4PORT
    /// (configured, or from the Perforce environment), encoded reversibly as
    /// a folder name (a long port is shortened with a hash).
    const std::string& serverKey() const { return _serverKey; }

    /// Lock file serializing changes to this workspace across processes.
    std::filesystem::path lockPath() const;

    /// Run any command on a pooled connection.
    p4::CommandResult run(const std::string& command,
                          const std::vector<std::string>& args = {},
                          const std::optional<std::string>& input = std::nullopt);

private:
    /// This server's and depot root's folder in the version store.
    std::filesystem::path _storeBase() const;

    Settings _settings;
    std::mutex _syncMutex;
    p4::ConnectionPool _pool;
    std::string _serverKey;
};

} // namespace clio::core
