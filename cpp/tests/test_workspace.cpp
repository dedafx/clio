// Integration tests against a throwaway p4d. Skipped without $CLIO_TEST_P4D.

#include "p4d_fixture.hpp"

#include "clio/core/error.hpp"
#include "clio/core/file_lock.hpp"
#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <thread>

using namespace clio::core;
using clio::test::P4dFixture;

namespace {

// File contents with CRLF line endings turned into LF: on Windows, Perforce
// writes text files with the platform's line endings.
std::string readFile(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::ostringstream out;
    out << in.rdbuf();
    std::string text = out.str();
    text.erase(std::remove(text.begin(), text.end(), '\r'), text.end());
    return text;
}

// An absolute path on this platform: "/work/proj" is not absolute on Windows.
std::string absPath(const std::string& posixPath) {
#ifdef _WIN32
    return "C:" + posixPath;
#else
    return posixPath;
#endif
}

// Settings for tests that need no server, rooted at /work/proj.
Settings offlineSettings() {
    return Settings::parse("depot=//d/main;root=" + absPath("/work/proj") + ";store=" + absPath("/cache/v") +
                           ";port=perf:1666");
}

std::unique_ptr<P4dFixture> fixtureOrSkip() {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
    }
    return fx;
}

} // namespace

TEST_CASE("connection runs tagged commands") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    const auto info = fx->connection().runOrThrow("info");
    REQUIRE(info.records.size() == 1);
    CHECK(info.records[0].at("userName") == "clio_tester");

    const auto bad = fx->connection().run("fstat", {"//depot/proj/does-not-exist"});
    CHECK(bad.records.empty());
    CHECK_THROWS_AS(fx->connection().runOrThrow("no-such-command"), P4Error);
}

TEST_CASE("latest syncs into the workspace") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    fx->submit({{"props/crate/crate.usda", "#usda 1.0\n"}}, "crate v1");
    fx->clearWorkspace();

    AssetResolver resolver(fx->settings());
    const auto id = AssetIdentifier::parse("clio:/props/crate/crate.usda");
    const auto resolved = resolver.resolve(id);
    REQUIRE(resolved);
    CHECK(resolved->localPath == fx->workspaceRoot() / "props/crate/crate.usda");
    CHECK(resolved->depotPath == "//depot/proj/props/crate/crate.usda");
    CHECK(readFile(resolved->localPath) == "#usda 1.0\n");

    CHECK_FALSE(resolver.resolve(AssetIdentifier::parse("clio:/props/missing.usda")));
}

TEST_CASE("historical pins go to the version store and leave the workspace alone") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    const auto v1 = fx->submit({{"props/crate/crate.usda", "v1"}}, "v1");
    fx->submit({{"props/crate/crate.usda", "v2"}}, "v2");

    AssetResolver resolver(fx->settings());
    const auto atV1 = resolver.resolve(
        AssetIdentifier::parse("clio:/props/crate/crate.usda?change=" + std::to_string(v1)));
    REQUIRE(atV1);
    CHECK(readFile(atV1->localPath) == "v1");
    CHECK(atV1->localPath.string().find(fx->versionStore().string()) == 0);

    const auto rev1 = resolver.resolve(AssetIdentifier::parse("clio:/props/crate/crate.usda?rev=1"));
    REQUIRE(rev1);
    CHECK(readFile(rev1->localPath) == "v1");

    CHECK(readFile(fx->workspaceRoot() / "props/crate/crate.usda") == "v2");
}

TEST_CASE("verify and offline policies never contact the server") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    fx->submit({{"a.usda", "a"}}, "a");
    fx->clearWorkspace();

    auto settings = fx->settings();
    settings.policy = Policy::Verify;
    AssetResolver verify(settings);
    CHECK_FALSE(verify.resolve(AssetIdentifier::parse("clio:/a.usda")));

    settings.policy = Policy::Offline;
    settings.connection.port = "localhost:1"; // would fail if contacted
    AssetResolver offline(settings);
    CHECK_FALSE(offline.resolve(AssetIdentifier::parse("clio:/a.usda")));
}

TEST_CASE("have pin uses the workspace without syncing") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    fx->submit({{"a.usda", "a1"}}, "a1");
    auto settings = fx->settings();
    settings.pin = Pin::have();
    AssetResolver resolver(settings);
    const auto resolved = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
    REQUIRE(resolved);
    CHECK(readFile(resolved->localPath) == "a1");
}

TEST_CASE("a moved label is fetched again by a new resolver") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    fx->submit({{"a.usda", "v1"}}, "v1");
    fx->connection().runOrThrow("tag", {"-l", "approved", "//depot/proj/..."});
    const auto id = AssetIdentifier::parse("clio:/a.usda?label=approved");
    {
        AssetResolver resolver(fx->settings());
        REQUIRE(resolver.resolve(id));
        CHECK(readFile(resolver.resolve(id)->localPath) == "v1");
    }

    fx->submit({{"a.usda", "v2"}}, "v2");
    fx->connection().runOrThrow("tag", {"-l", "approved", "//depot/proj/..."});
    {
        AssetResolver resolver(fx->settings()); // a new process, in effect
        const auto resolved = resolver.resolve(id);
        REQUIRE(resolved);
        CHECK(readFile(resolved->localPath) == "v2");
    }
}

TEST_CASE("verify policy uses a stored historical version without the server") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    const auto v1 = fx->submit({{"a.usda", "v1"}}, "v1");
    const auto id = AssetIdentifier::parse("clio:/a.usda?change=" + std::to_string(v1));
    REQUIRE(AssetResolver(fx->settings()).resolve(id)); // fetch into the store

    // The version store is organised per server, so keep the same port;
    // the verify policy has no code path that contacts the server.
    auto settings = fx->settings();
    settings.policy = Policy::Verify;
    AssetResolver verify(settings);
    const auto resolved = verify.resolve(id);
    REQUIRE(resolved);
    CHECK(readFile(resolved->localPath) == "v1");
}

TEST_CASE("when Perforce is unavailable, local files are used with a warning") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    const auto v1 = fx->submit({{"a.usda", "v1"}}, "v1");
    fx->submit({{"a.usda", "v2"}}, "v2"); // the workspace now has v2

    auto settings = fx->settings();
    settings.connection.port = "localhost:1"; // nothing listens here
    settings.pin = Pin::latest();             // a pin that needs the server
    AssetResolver resolver(settings);

    SUBCASE("latest uses the workspace file") {
        const auto resolved = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
        REQUIRE(resolved);
        CHECK(readFile(resolved->localPath) == "v2");
        CHECK(resolved->warning.find("Perforce is not available") != std::string::npos);
    }
    SUBCASE("a historical pin falls back to the workspace file") {
        const auto resolved = resolver.resolve(
            AssetIdentifier::parse("clio:/a.usda?change=" + std::to_string(v1)));
        REQUIRE(resolved);
        CHECK(resolved->localPath == fx->workspaceRoot() / "a.usda");
        CHECK(resolved->warning.find("may not be version @" + std::to_string(v1)) != std::string::npos);
    }
    SUBCASE("a file that is not on disk still fails") {
        CHECK_FALSE(resolver.resolve(AssetIdentifier::parse("clio:/missing.usda")));
    }
    SUBCASE("verify policy never falls back") {
        auto strict = settings;
        strict.policy = Policy::Verify;
        AssetResolver verify(strict);
        CHECK_FALSE(verify.resolve(AssetIdentifier::parse("clio:/a.usda?change=" + std::to_string(v1))));
    }
}

TEST_CASE("an unreachable server does not stall resolves") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");

    auto settings = fx->settings();
    settings.connection.port = "10.255.255.1:1666"; // non-routable: packets are dropped
    settings.connection.connectTimeout = std::chrono::seconds(2);
    settings.pin = Pin::latest(); // a pin that needs the server
    AssetResolver resolver(settings);

    const auto start = std::chrono::steady_clock::now();
    const auto first = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
    const auto second = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
    const auto elapsed = std::chrono::steady_clock::now() - start;

    REQUIRE(first);
    REQUIRE(second);
    CHECK_FALSE(first->warning.empty());
    // One connect attempt at most (2 s); the second resolve does not retry.
    CHECK(elapsed < std::chrono::seconds(8));
}

TEST_CASE("local paths map back to project files") {
    Workspace ws(offlineSettings());
    const auto storeDir = ws.versionStorePath(AssetIdentifier::parse("clio:/x"), Pin::change(7)).parent_path();

    const auto inWorkspace = ws.matchLocalPath(absPath("/work/proj/assets/crate/../crate/crate.usda"));
    REQUIRE(inWorkspace);
    CHECK(inWorkspace->id.path() == "/assets/crate/crate.usda");
    CHECK_FALSE(inWorkspace->pin);
    CHECK_FALSE(inWorkspace->inVersionStore);

    const auto inStore = ws.matchLocalPath(storeDir / "assets/crate/geo.usda");
    REQUIRE(inStore);
    CHECK(inStore->id.path() == "/assets/crate/geo.usda");
    CHECK(*inStore->pin == Pin::change(7));
    CHECK(inStore->inVersionStore);

    CHECK_FALSE(ws.matchLocalPath(absPath("/elsewhere/a.usda")));
    CHECK_FALSE(ws.matchLocalPath(absPath("/work/proj")));
    CHECK_FALSE(ws.matchLocalPath(absPath("/work/project2/a.usda")));
    CHECK_FALSE(ws.matchLocalPath("relative/a.usda"));
}

#ifdef _WIN32
TEST_CASE("Windows paths match whatever the case and separators") {
    Workspace ws(offlineSettings());
    const auto match = ws.matchLocalPath(R"(c:\WORK\Proj\Assets\crate.usda)");
    REQUIRE(match);
    // The project path keeps the spelling it was given.
    CHECK(match->id.path() == "/Assets/crate.usda");
    CHECK_FALSE(ws.matchLocalPath("D:/work/proj/a.usda"));
}
#endif

TEST_CASE("a relative file next to a pinned layer is fetched at the same version") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    const auto v1 = fx->submit({{"assets/crate/crate.usda", "crate v1"}, {"assets/crate/geo.usda", "geo v1"}}, "v1");
    fx->submit({{"assets/crate/geo.usda", "geo v2"}}, "v2");

    AssetResolver resolver(fx->settings());
    const auto crate = resolver.resolve(
        AssetIdentifier::parse("clio:/assets/crate/crate.usda?change=" + std::to_string(v1)));
    REQUIRE(crate);
    // What USD does with @./geo.usda@ inside the pinned crate layer:
    const auto geo = resolver.resolvePath(crate->localPath.parent_path() / "geo.usda");
    REQUIRE(geo);
    CHECK(readFile(geo->localPath) == "geo v1");
}

TEST_CASE("paths outside the project are not handled") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    AssetResolver resolver(fx->settings());
    CHECK_FALSE(resolver.manages(absPath("/tmp/elsewhere.usda")));
    CHECK_FALSE(resolver.resolvePath(absPath("/tmp/elsewhere.usda")));
}

TEST_CASE("label folders are collision-free and map back to the label") {
    Workspace ws(offlineSettings());
    const auto id = AssetIdentifier::parse("clio:/a.usda");
    const auto colon = ws.versionStorePath(id, Pin::label("approved:prod"));
    const auto underscore = ws.versionStorePath(id, Pin::label("approved_prod"));
    CHECK(colon != underscore);
    // Different folders even on case-insensitive file systems.
    const auto upper = ws.versionStorePath(id, Pin::label("Prod"));
    const auto lower = ws.versionStorePath(id, Pin::label("prod"));
    CHECK(upper.parent_path().filename() == "label-%50rod");
    CHECK(lower.parent_path().filename() == "label-prod");
    REQUIRE(ws.matchLocalPath(upper));
    CHECK(*ws.matchLocalPath(upper)->pin == Pin::label("Prod"));

    const auto match = ws.matchLocalPath(colon);
    REQUIRE(match);
    CHECK(*match->pin == Pin::label("approved:prod"));
    // Non-canonical folder names are not treated as labels.
    CHECK_FALSE(ws.matchLocalPath(colon.parent_path().parent_path() / "label-approved%3aprod" / "a.usda"));
    CHECK_FALSE(ws.matchLocalPath(colon.parent_path().parent_path() / "label-bad%4" / "a.usda"));
}

TEST_CASE("an empty port uses the effective P4PORT for the store folder") {
#ifdef _WIN32
    _putenv_s("P4PORT", "envserver:1666");
#else
    setenv("P4PORT", "envserver:1666", 1);
#endif
    Workspace ws(Settings::parse("depot=//d/main;root=" + absPath("/work/proj") + ";store=" + absPath("/cache/v")));
    CHECK(ws.serverKey() == "envserver%3A1666");
#ifdef _WIN32
    _putenv_s("P4PORT", "");
#else
    unsetenv("P4PORT");
#endif
}

TEST_CASE("store folders never mix depot roots or servers") {
    const auto id = AssetIdentifier::parse("clio:/a.usda");
    const auto store = [&](const std::string& depot, const std::string& port) {
        Workspace ws(Settings::parse("depot=" + depot + ";root=" + absPath("/work/proj") +
                                     ";store=" + absPath("/cache/v") + ";port=" + port));
        return ws.versionStorePath(id, Pin::change(7));
    };
    // "//d/main" and "//d_main" used to share the folder "d_main".
    const auto slash = store("//d/main", "perf:1666");
    const auto underscore = store("//d_main", "perf:1666");
    CHECK(slash != underscore);
    Workspace mainWs(Settings::parse("depot=//d_main;root=" + absPath("/work/proj") + ";store=" +
                                     absPath("/cache/v") + ";port=perf:1666"));
    CHECK_FALSE(mainWs.matchLocalPath(slash));

    // A long port (an rsh: command line) is shortened, and stays unique.
    const std::string longPort = "rsh:/opt/perforce/bin/p4d -r /var/lib/perforce/servers/";
    const auto a = store("//d/main", longPort + "alpha -L log -J off -i");
    const auto b = store("//d/main", longPort + "bravo -L log -J off -i");
    CHECK(a != b);
    const auto serverFolder = a.parent_path().parent_path().parent_path().filename().string();
    CHECK(serverFolder.size() <= 64);
    CHECK(serverFolder.find('~') != std::string::npos);
}

TEST_CASE("depot paths escape the characters Perforce reserves") {
    Workspace ws(offlineSettings());
    CHECK(ws.depotPath(AssetIdentifier::parse("clio:/tex/crate@2x#1%*.png")) ==
          "//d/main/tex/crate%402x%231%25%2A.png");
}

TEST_CASE("files with reserved characters in their names resolve") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    const auto v1 = fx->submit({{"tex/crate@2x.usda", "v1"}, {"tex/50%.usda", "p1"}}, "v1");
    fx->submit({{"tex/other@2x.usda", "other"}}, "v2"); // must not be fetched below
    fx->clearWorkspace();

    AssetResolver resolver(fx->settings());
    const auto latest = resolver.resolve(AssetIdentifier::parse("clio:/tex/crate@2x.usda"));
    REQUIRE(latest);
    CHECK(latest->localPath == fx->workspaceRoot() / "tex" / "crate@2x.usda");
    CHECK(readFile(latest->localPath) == "v1");
    CHECK_FALSE(std::filesystem::exists(fx->workspaceRoot() / "tex" / "other@2x.usda"));

    const auto pinned = resolver.resolve(AssetIdentifier::parse("clio:/tex/50%.usda?change=" + std::to_string(v1)));
    REQUIRE(pinned);
    CHECK(readFile(pinned->localPath) == "p1");
}

TEST_CASE("a stored label used while the server is unavailable carries a warning") {
    const auto store = std::filesystem::temp_directory_path() /
                       ("clio-label-test-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    auto settings = Settings::parse("depot=//d/main;root=" + absPath("/work/proj") + ";port=localhost:1");
    settings.versionStore = store;
    settings.connection.connectTimeout = std::chrono::seconds(2);

    // Copies stored earlier, when the server was reachable.
    Workspace ws(settings);
    for (const char* name : {"a.usda", "b.usda"}) {
        const auto path = ws.versionStorePath(AssetIdentifier::parse(std::string("clio:/") + name), Pin::label("approved"));
        std::filesystem::create_directories(path.parent_path());
        std::ofstream(path) << "stored";
    }

    AssetResolver resolver(settings);
    // The first resolve tries the server, fails and starts the retry interval.
    const auto a = resolver.resolve(AssetIdentifier::parse("clio:/a.usda?label=approved"));
    REQUIRE(a);
    CHECK_FALSE(a->warning.empty());
    // During the retry interval the server is not asked, but the answer is
    // still flagged and not remembered.
    for (int i = 0; i < 2; ++i) {
        const auto b = resolver.resolve(AssetIdentifier::parse("clio:/b.usda?label=approved"));
        REQUIRE(b);
        CHECK(b->warning.find("may be out of date") != std::string::npos);
    }

    std::error_code ec;
    std::filesystem::remove_all(store, ec);
}

TEST_CASE("stored versions are read-only") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    const auto v1 = fx->submit({{"a.usda", "v1"}}, "v1");
    AssetResolver resolver(fx->settings());
    const auto resolved = resolver.resolve(AssetIdentifier::parse("clio:/a.usda?change=" + std::to_string(v1)));
    REQUIRE(resolved);
    const auto perms = std::filesystem::status(resolved->localPath).permissions();
    CHECK((perms & std::filesystem::perms::owner_write) == std::filesystem::perms::none);
}

TEST_CASE("a file missing at a pinned version is not a server failure") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    const auto v1 = fx->submit({{"a.usda", "v1"}}, "v1");
    AssetResolver resolver(fx->settings());
    CHECK_FALSE(resolver.resolve(
        AssetIdentifier::parse("clio:/missing.usda?change=" + std::to_string(v1))));
    // The server is still used for the next request (no fallback warning).
    const auto next = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
    REQUIRE(next);
    CHECK(next->warning.empty());
}

TEST_CASE("the workspace lock excludes other holders until released") {
    const auto path = std::filesystem::temp_directory_path() / ("clio-lock-test-" + std::to_string(std::rand()));
    std::atomic<bool> secondAcquired{false};
    std::thread second;
    {
        FileLock first(path);
        second = std::thread([&] {
            FileLock again(path); // a separate open file: blocks like another process would
            secondAcquired = true;
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        CHECK_FALSE(secondAcquired.load());
    }
    second.join();
    CHECK(secondAcquired.load());
    std::filesystem::remove(path);
}

TEST_CASE("have: a file on disk is loaded as it is; a file not on disk is synced") {
    auto fx = fixtureOrSkip();
    if (!fx) return;

    fx->submit({{"a.usda", "a1"}, {"b.usda", "b1"}, {"c.usda", "c1"}}, "v1");
    fx->submit({{"a.usda", "a2"}, {"b.usda", "b2"}, {"c.usda", "c2"}}, "v2");
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/a.usda#1"});    // on disk, older than head
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/b.usda#none"}); // never synced here
    std::filesystem::remove(fx->workspaceRoot() / "c.usda");                 // deleted outside Perforce

    AssetResolver resolver(fx->settings()); // default pin: have
    REQUIRE(resolver.settings().pin == Pin::have());

    const auto a = resolver.resolve(AssetIdentifier::parse("clio:/a.usda"));
    REQUIRE(a);
    CHECK(readFile(a->localPath) == "a1"); // the version on disk wins over head

    const auto b = resolver.resolve(AssetIdentifier::parse("clio:/b.usda"));
    REQUIRE(b);
    CHECK(readFile(b->localPath) == "b2"); // missing: synced at head

    const auto c = resolver.resolve(AssetIdentifier::parse("clio:/c.usda"));
    REQUIRE(c);
    CHECK(readFile(c->localPath) == "c2"); // deleted: the workspace's revision restored

    CHECK_FALSE(resolver.resolve(AssetIdentifier::parse("clio:/not-in-perforce.usda")));
}

TEST_CASE("have with the offline policy never syncs") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "a1"}}, "v1");
    fx->clearWorkspace();
    auto settings = fx->settings();
    settings.policy = Policy::Offline;
    AssetResolver resolver(settings);
    CHECK_FALSE(resolver.resolve(AssetIdentifier::parse("clio:/a.usda")));
    CHECK_FALSE(std::filesystem::exists(fx->workspaceRoot() / "a.usda"));
}
