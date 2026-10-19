// Revision-based modification timestamps (design doc §10.3,
// _GetModificationTimestamp). Needs $CLIO_TEST_P4D.

#include "p4d_fixture.hpp"

#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

using namespace clio::core;
using clio::test::P4dFixture;

namespace {

// A second workspace of the test user, with the `modtime` option.
struct ModtimeClient {
    std::filesystem::path root;
    Settings settings;
    std::unique_ptr<p4::Connection> connection;

    ModtimeClient(P4dFixture& fx, const std::string& name, const std::string& options) {
        root = fx.workspaceRoot().parent_path() / name;
        std::filesystem::create_directories(root);
        fx.connection().runOrThrow(
            "client", {"-i"},
            "Client: " + name + "\nOwner: clio_tester\nRoot: " + root.string() + "\nOptions: " + options +
                "\nLineEnd: local\nView:\n\t//depot/proj/... //" + name + "/...\n");
        settings = fx.settings();
        settings.connection.client = name;
        settings.connection.cwd = root.string();
        settings.workspaceRoot = root;
        connection = std::make_unique<p4::Connection>(settings.connection);
    }
};

const char* const withModtime = "allwrite noclobber nocompress unlocked modtime rmdir";
const char* const withoutModtime = "allwrite noclobber nocompress unlocked nomodtime rmdir";

} // namespace

TEST_CASE("a modtime client gets the workspace revision as its timestamp") {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
        return;
    }
    fx->submit({{"a.usda", "v1"}}, "v1");
    fx->submit({{"a.usda", "v2"}}, "v2");

    ModtimeClient client(*fx, "mt_ws", withModtime);
    client.connection->runOrThrow("sync", {"-q", "//depot/proj/a.usda#1"});
    client.settings.pin = Pin::latest();
    AssetResolver resolver(client.settings);
    const auto path = client.root / "a.usda";

    CHECK(resolver.revisionTimestamp(path) == 1.0);

    // Another tool syncs the head revision. After a refresh the new revision shows.
    client.connection->runOrThrow("sync", {"-q", "//depot/proj/a.usda"});
    CHECK(resolver.revisionTimestamp(path) == 1.0); // remembered for cache_ttl
    resolver.refresh();
    CHECK(resolver.revisionTimestamp(path) == 2.0);

    // A sync done by the resolver itself drops what it knew, with no refresh.
    client.connection->runOrThrow("sync", {"-q", "//depot/proj/a.usda#1"});
    resolver.refresh();
    CHECK(resolver.revisionTimestamp(path) == 1.0);
    const auto id = AssetIdentifier::parse("clio:/a.usda");
    REQUIRE(resolver.resolve(id)); // pin=latest: syncs the head revision
    CHECK(resolver.revisionTimestamp(path) == 2.0);

    // Open for edit: the local file may differ, so its own time is used.
    CHECK(resolver.prepareForWrite(id).allowed);
    CHECK_FALSE(resolver.revisionTimestamp(path).has_value());
}

TEST_CASE("without modtime, or off the server, the file's own time is used") {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
        return;
    }
    fx->submit({{"a.usda", "v1"}}, "v1");

    // The fixture's client has nomodtime.
    AssetResolver plain(fx->settings());
    CHECK_FALSE(plain.revisionTimestamp(fx->workspaceRoot() / "a.usda").has_value());

    ModtimeClient client(*fx, "mt_ws2", withModtime);
    client.connection->runOrThrow("sync", {"-q", "//depot/proj/a.usda"});

    AssetResolver resolver(client.settings);
    CHECK(resolver.revisionTimestamp(client.root / "a.usda").has_value());
    // Not a project file, and a stored version.
    CHECK_FALSE(resolver.revisionTimestamp(client.root.parent_path() / "elsewhere.usda").has_value());
    CHECK_FALSE(resolver.revisionTimestamp(fx->versionStore() / "x").has_value());

    for (const Policy policy : {Policy::Verify, Policy::Offline}) {
        auto settings = client.settings;
        settings.policy = policy;
        AssetResolver noServer(settings);
        CHECK_FALSE(noServer.revisionTimestamp(client.root / "a.usda").has_value());
    }

    ModtimeClient other(*fx, "nomt_ws", withoutModtime);
    other.connection->runOrThrow("sync", {"-q", "//depot/proj/a.usda"});
    AssetResolver other_resolver(other.settings);
    CHECK_FALSE(other_resolver.revisionTimestamp(other.root / "a.usda").has_value());
}

TEST_CASE("an unreachable server falls back to the file's own time") {
    auto settings = Settings::parse("depot=//d/main;root=" +
                                    (std::filesystem::temp_directory_path() / "clio-nowhere2").string() +
                                    ";port=localhost:1");
    settings.connection.connectTimeout = std::chrono::seconds(2);
    AssetResolver resolver(settings);
    CHECK_FALSE(resolver.revisionTimestamp(settings.workspaceRoot / "a.usda").has_value());
}
