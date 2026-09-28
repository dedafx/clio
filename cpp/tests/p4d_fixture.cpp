#include "p4d_fixture.hpp"

#include <atomic>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <vector>

namespace clio::test {

namespace {

constexpr const char* testUser = "clio_tester";
constexpr const char* testClient = "clio_test_ws";
constexpr const char* testPassword = "ClioTest-1!";

std::filesystem::path makeTempDir() {
    static std::atomic<unsigned> counter{0};
    const auto base = std::filesystem::temp_directory_path();
    for (int attempt = 0; attempt < 100; ++attempt) {
        auto dir = base / ("clio-test-" + std::to_string(std::rand()) + "-" +
                           std::to_string(counter.fetch_add(1)));
        if (std::filesystem::create_directory(dir)) {
            return dir;
        }
    }
    throw std::runtime_error("could not create a temporary directory");
}

} // namespace

std::unique_ptr<P4dFixture> P4dFixture::create() {
    const char* p4d = std::getenv("CLIO_TEST_P4D");
    if (!p4d || !*p4d) {
        return nullptr;
    }

    std::unique_ptr<P4dFixture> fx(new P4dFixture());
    fx->_dir = makeTempDir();
    fx->_workspaceRoot = fx->_dir / "ws";
    fx->_versionStore = fx->_dir / "store";
    const auto serverRoot = fx->_dir / "server";
    std::filesystem::create_directories(fx->_workspaceRoot);
    std::filesystem::create_directories(serverRoot);

    fx->_port = "rsh:" + std::string(p4d) + " -r " + serverRoot.string() + " -L log -J off -i";

    fx->_ticketFile = (fx->_dir / "tickets").string();

    core::p4::ConnectionOptions options;
    options.port = fx->_port;
    options.user = testUser;
    options.client = testClient;
    options.cwd = fx->_workspaceRoot.string();
    options.ticketFile = fx->_ticketFile;

    // Recent servers require every user to have a password, even on a new
    // server. Set one and log in; the ticket then serves every connection.
    {
        auto setup = options;
        setup.prompt = [](const std::string&, bool) { return std::optional<std::string>(testPassword); };
        core::p4::Connection admin(setup);
        admin.runOrThrow("passwd");
        admin.runOrThrow("login");
    }

    fx->_connection = std::make_unique<core::p4::Connection>(options);

    const std::string spec =
        "Client: " + std::string(testClient) + "\n"
        "Owner: " + testUser + "\n"
        "Root: " + fx->_workspaceRoot.string() + "\n"
        "Options: allwrite noclobber nocompress unlocked nomodtime normdir\n"
        "LineEnd: local\n"
        "View:\n"
        "\t//depot/proj/... //" + testClient + "/...\n";
    fx->_connection->runOrThrow("client", {"-i"}, spec);
    return fx;
}

P4dFixture::~P4dFixture() {
    _connection.reset();
    std::error_code ec;
    std::filesystem::remove_all(_dir, ec);
}

core::Settings P4dFixture::settings() const {
    core::Settings s;
    s.depotRoot = depotRoot();
    s.workspaceRoot = _workspaceRoot;
    s.versionStore = _versionStore;
    s.connection.port = _port;
    s.connection.user = testUser;
    s.connection.client = testClient;
    s.connection.ticketFile = _ticketFile;
    return s;
}

unsigned long P4dFixture::submit(const std::map<std::string, std::string>& files,
                                 const std::string& description) {
    std::vector<std::string> paths;
    for (const auto& [relative, content] : files) {
        const auto local = _workspaceRoot / relative;
        const bool exists = std::filesystem::exists(local);
        if (exists) {
            _connection->runOrThrow("edit", {local.string()});
        }
        std::filesystem::create_directories(local.parent_path());
        std::ofstream(local, std::ios::binary | std::ios::trunc) << content;
        if (!exists) {
            _connection->runOrThrow("add", {local.string()});
        }
    }
    const auto result = _connection->runOrThrow("submit", {"-d", description});
    for (const auto& record : result.records) {
        if (auto it = record.find("submittedChange"); it != record.end()) {
            return std::stoul(it->second);
        }
    }
    throw std::runtime_error("submit did not report a change number");
}

void P4dFixture::clearWorkspace() {
    _connection->runOrThrow("sync", {"-q", depotRoot() + "/...#none"});
}

} // namespace clio::test
