#pragma once

#include "clio/core/p4/connection.hpp"
#include "clio/core/pin.hpp"

#include <chrono>
#include <cstddef>
#include <filesystem>
#include <string>

namespace clio::core {

/// What the resolver may do when an asset is missing or out of date
/// (design doc §10.3).
enum class Policy {
    Sync,    ///< Fetch what is needed from the server (default).
    Verify,  ///< Never contact the server; fail if the file is not present.
    Offline, ///< Never contact the server; use what is on disk.
};

std::string policyName(Policy policy);
Policy parsePolicy(const std::string& text);

/// Everything needed to resolve clio: identifiers for one project.
///
/// This is also the text form of a USD resolver context, as
/// semicolon-separated key=value pairs, for example:
///
///   depot=//imagine/main;root=/work/imagine;client=sam_imagine;pin=@18234
///
/// Keys:
///   depot    depot path of the project root (required), e.g. //imagine/main
///   root     local directory the client workspace maps `depot` to (required)
///   store    local directory for historical versions (default: user cache)
///   port, user, client, tickets
///            Perforce settings; empty means the standard P4 environment
///   pin      default pin: have (default), latest, @change, @label
///   policy   sync (default), verify, offline
///   timeout  command timeout in seconds (default 120, 0 = none)
///   connect_timeout
///            seconds to wait for an unreachable server (default 10, 0 = OS default)
///   connections
///            most Perforce connections the process opens (default 4); they
///            are opened only when threads need them at the same time
///   batch_ms milliseconds a resolve that needs the server waits for other
///            misses to join its `sync` (default 20, 0 = do not wait)
///   cache_ttl
///            seconds a resolve of a moving target (have, latest, label) is
///            remembered (default 60, 0 = check every time). Changes and
///            revisions never change and are remembered until refresh()
struct Settings {
    p4::ConnectionOptions connection;
    std::string depotRoot;
    std::filesystem::path workspaceRoot;
    std::filesystem::path versionStore;
    Pin pin = Pin::have();
    Policy policy = Policy::Sync;
    std::size_t maxConnections = 4;
    std::chrono::milliseconds batchWindow{20};
    std::chrono::seconds cacheTtl{60};

    /// Parse the text form. Throws ConfigError or PinError.
    static Settings parse(const std::string& text);

    /// Canonical text form: fixed key order, defaults omitted. Two Settings
    /// with the same canonical form resolve identically.
    std::string str() const;

    bool operator==(const Settings& other) const { return str() == other.str(); }
    bool operator!=(const Settings& other) const { return !(*this == other); }

    /// The default version store: $CLIO_VERSION_STORE, else
    /// <defaultCacheDir>/versions.
    static std::filesystem::path defaultVersionStore();

    /// Clio's per-user cache folder: $XDG_CACHE_HOME/clio or ~/.cache/clio
    /// (%LOCALAPPDATA%\clio on Windows). Holds lock files and, by default,
    /// the version store.
    static std::filesystem::path defaultCacheDir();
};

} // namespace clio::core
