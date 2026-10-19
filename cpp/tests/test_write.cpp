// The write side of the resolver (design doc §10.6): opening files for edit
// and add, and refusing a write that someone else's lock forbids. Needs
// $CLIO_TEST_P4D; the last test needs no server.

#include "p4d_fixture.hpp"

#include "clio/core/resolver.hpp"

#include <doctest/doctest.h>

#include <fstream>

using namespace clio::core;
using clio::test::P4dFixture;

namespace {

std::unique_ptr<P4dFixture> fixtureOrSkip() {
    auto fx = P4dFixture::create();
    if (!fx) {
        MESSAGE("CLIO_TEST_P4D is not set; skipping Perforce integration test");
    }
    return fx;
}

// The "action" of a depot file in this workspace ("" if it is not open).
std::string openedAs(P4dFixture& fx, const std::string& relative) {
    const auto result = fx.connection().run("opened", {"//depot/proj/" + relative});
    return result.records.empty() ? std::string() : result.records[0].at("action");
}

AssetIdentifier idOf(const std::string& relative) {
    return AssetIdentifier::parse("clio:/" + relative);
}

} // namespace

TEST_CASE("writing an existing file opens it for edit") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");

    AssetResolver resolver(fx->settings());
    const auto check = resolver.prepareForWrite(idOf("a.usda"));
    CHECK(check.allowed);
    CHECK_FALSE(check.isNew);
    CHECK(check.warning.empty());
    CHECK(openedAs(*fx, "a.usda") == "edit");

    // A second save finds it open already.
    CHECK(resolver.prepareForWrite(idOf("a.usda")).allowed);
    CHECK(openedAs(*fx, "a.usda") == "edit");
}

TEST_CASE("a new file is opened for add once it has been written") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");

    AssetResolver resolver(fx->settings());
    const auto id = idOf("new/layer.usda");
    const auto check = resolver.prepareForWrite(id);
    CHECK(check.allowed);
    CHECK(check.isNew);
    CHECK(openedAs(*fx, "new/layer.usda").empty());

    // Nothing was written: nothing to add.
    CHECK(resolver.finishWrite(id).empty());
    CHECK(openedAs(*fx, "new/layer.usda").empty());

    const auto path = fx->workspaceRoot() / "new" / "layer.usda";
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << "#usda 1.0\n";
    CHECK(resolver.finishWrite(id).empty());
    CHECK(openedAs(*fx, "new/layer.usda") == "add");
}

TEST_CASE("a lock held by someone else refuses the write") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");

    // Another workspace of the same user locks the file.
    const auto otherRoot = fx->workspaceRoot().parent_path() / "other_ws";
    std::filesystem::create_directories(otherRoot);
    const std::string spec =
        "Client: other_ws\nOwner: clio_tester\nRoot: " + otherRoot.string() +
        "\nOptions: allwrite noclobber nocompress unlocked nomodtime normdir\nLineEnd: local\n"
        "View:\n\t//depot/proj/... //other_ws/...\n";
    fx->connection().runOrThrow("client", {"-i"}, spec);
    auto options = fx->settings().connection;
    options.client = "other_ws";
    options.cwd = otherRoot.string();
    p4::Connection other(options);
    other.runOrThrow("sync", {"-q", "//depot/proj/a.usda"});
    other.runOrThrow("edit", {"//depot/proj/a.usda"});
    other.runOrThrow("lock", {"//depot/proj/a.usda"});

    AssetResolver resolver(fx->settings());
    const auto check = resolver.prepareForWrite(idOf("a.usda"));
    CHECK_FALSE(check.allowed);
    CHECK(check.whyNot.find("locked by clio_tester (workspace other_ws)") != std::string::npos);
    CHECK(openedAs(*fx, "a.usda").empty());

    other.runOrThrow("revert", {"//depot/proj/a.usda"});
    CHECK(resolver.prepareForWrite(idOf("a.usda")).allowed);
    CHECK(openedAs(*fx, "a.usda") == "edit");
}

TEST_CASE("a file that is not in the workspace is refused, an old copy is a warning") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}, {"b.usda", "b1"}}, "v1");
    fx->submit({{"a.usda", "v2"}}, "v2");
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/a.usda#1"});
    fx->connection().runOrThrow("sync", {"-q", "//depot/proj/b.usda#none"});

    AssetResolver resolver(fx->settings());
    const auto notThere = resolver.prepareForWrite(idOf("b.usda"));
    CHECK_FALSE(notThere.allowed);
    CHECK(notThere.whyNot.find("not in your workspace") != std::string::npos);

    const auto old = resolver.prepareForWrite(idOf("a.usda"));
    CHECK(old.allowed);
    CHECK(old.warning.find("not the latest") != std::string::npos);
    CHECK(openedAs(*fx, "a.usda") == "edit");
}

TEST_CASE("verify and offline policies never open files") {
    auto fx = fixtureOrSkip();
    if (!fx) return;
    fx->submit({{"a.usda", "v1"}}, "v1");

    for (const Policy policy : {Policy::Verify, Policy::Offline}) {
        auto settings = fx->settings();
        settings.policy = policy;
        AssetResolver resolver(settings);
        const auto check = resolver.prepareForWrite(idOf("a.usda"));
        CHECK(check.allowed);
        CHECK(check.warning.empty());
        CHECK(openedAs(*fx, "a.usda").empty());
    }
}

TEST_CASE("an unreachable server does not block a write") {
    auto settings = Settings::parse("depot=//d/main;root=" +
                                    (std::filesystem::temp_directory_path() / "clio-nowhere").string() +
                                    ";port=localhost:1");
    settings.connection.connectTimeout = std::chrono::seconds(2);
    AssetResolver resolver(settings);
    const auto check = resolver.prepareForWrite(idOf("a.usda"));
    CHECK(check.allowed);
    CHECK_FALSE(check.warning.empty());
    // The server is not tried again at once.
    CHECK(resolver.finishWrite(idOf("a.usda")).empty());
}
