#pragma once

#include "clio/core/p4/connection.hpp"
#include "clio/core/settings.hpp"

#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <string>

namespace clio::test {

/// A throwaway Perforce server with one user and one client workspace.
///
/// The server runs through an rsh: port (p4d -i), so there is no network
/// listener. It needs a p4d binary, found through $CLIO_TEST_P4D.
class P4dFixture {
public:
    /// Returns nullopt (and the caller skips) if $CLIO_TEST_P4D is not set.
    static std::unique_ptr<P4dFixture> create();

    ~P4dFixture();

    const std::filesystem::path& workspaceRoot() const { return _workspaceRoot; }
    const std::filesystem::path& versionStore() const { return _versionStore; }
    std::string depotRoot() const { return "//depot/proj"; }

    /// Settings for an AssetResolver or Workspace on this server.
    core::Settings settings() const;

    /// Write files into the workspace, open them for add or edit, and submit.
    /// Returns the submitted change number.
    unsigned long submit(const std::map<std::string, std::string>& files, const std::string& description);

    /// Remove the local copies of every file (p4 sync #none).
    void clearWorkspace();

    core::p4::Connection& connection() { return *_connection; }

private:
    P4dFixture() = default;

    std::filesystem::path _dir;
    std::filesystem::path _workspaceRoot;
    std::filesystem::path _versionStore;
    std::string _port;
    std::string _ticketFile;
    std::unique_ptr<core::p4::Connection> _connection;
};

} // namespace clio::test
