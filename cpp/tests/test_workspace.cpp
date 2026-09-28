// Integration tests against a throwaway p4d. Skipped without $CLIO_TEST_P4D.

#include "p4d_fixture.hpp"

#include "clio/core/error.hpp"
#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

#include <chrono>
#include <fstream>
#include <sstream>

using namespace clio::core;
using clio::test::P4dFixture;

namespace {

std::string readFile(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::ostringstream out;
    out << in.rdbuf();
    return out.str();
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
