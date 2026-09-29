#include "clio/core/error.hpp"
#include "clio/core/identifier.hpp"

#include <doctest/doctest.h>

using clio::core::AssetIdentifier;
using clio::core::IdentifierError;
using clio::core::Pin;

TEST_CASE("identifiers parse and normalize") {
    CHECK(AssetIdentifier::parse("clio:/props/crate/crate.usd").str() == "clio:/props/crate/crate.usd");
    CHECK(AssetIdentifier::parse("CLIO:/props/crate.usd").str() == "clio:/props/crate.usd");
    CHECK(AssetIdentifier::parse("clio:///props//./crate/../crate.usd").path() == "/props/crate.usd");
    CHECK(AssetIdentifier::parse("clio:/props\\crate\\crate.usd").path() == "/props/crate/crate.usd");
    CHECK(AssetIdentifier::parse("clio:/a/b.usd").relativePath() == "a/b.usd");
}

TEST_CASE("identifier pins come from the query") {
    CHECK_FALSE(AssetIdentifier::parse("clio:/a.usd").pin().has_value());
    CHECK(*AssetIdentifier::parse("clio:/a.usd?change=42").pin() == Pin::change(42));
    CHECK(*AssetIdentifier::parse("clio:/a.usd?label=approved").pin() == Pin::label("approved"));
    CHECK(*AssetIdentifier::parse("clio:/a.usd?rev=3").pin() == Pin::revision(3));
    CHECK(AssetIdentifier::parse("clio:/a.usd?change=42").str() == "clio:/a.usd?change=42");
}

TEST_CASE("invalid identifiers are rejected") {
    for (const char* text : {"/props/crate.usd", "clio:", "clio:props/crate.usd", "clio:/",
                             "clio:/..", "clio:/a/../../b.usd", "clio://server/a.usd",
                             "clio:/a.usd?pin=@3", "clio:/a.usd?change=1&rev=2", "clio:/a.usd?change"}) {
        CAPTURE(text);
        CHECK_THROWS_AS(AssetIdentifier::parse(text), clio::core::Error);
    }
}

TEST_CASE("identifiers name one file inside the project") {
    // "..." is a Perforce wildcard: syncing it would fetch a whole folder.
    // A drive would make the path leave the workspace root on Windows.
    for (const char* text : {"clio:/...", "clio:/props/...", "clio:/props/a...b.usd", "clio:/C:/outside.usda",
                             "clio:/c:\\outside.usda", "clio:/a/../D:/x.usd"}) {
        CAPTURE(text);
        CHECK_THROWS_AS(AssetIdentifier::parse(text), IdentifierError);
    }
    CHECK_THROWS_AS(AssetIdentifier::fromRelativePath("C:/outside.usda"), IdentifierError);
    // Characters Perforce reserves are allowed; depot paths escape them.
    CHECK(AssetIdentifier::parse("clio:/tex/crate@2x.png").path() == "/tex/crate@2x.png");
    CHECK(AssetIdentifier::parse("clio:/a..b/c.usd").path() == "/a..b/c.usd");
}

TEST_CASE("relative paths anchor to the anchor's directory") {
    const auto anchor = AssetIdentifier::parse("clio:/props/crate/crate.usd");
    CHECK(AssetIdentifier::anchor("./geo/crate_geo.usdc", anchor).str() ==
          "clio:/props/crate/geo/crate_geo.usdc");
    CHECK(AssetIdentifier::anchor("../shared/mat.usd", anchor).str() == "clio:/props/shared/mat.usd");
    CHECK(AssetIdentifier::anchor("tex.exr", anchor).str() == "clio:/props/crate/tex.exr");
    CHECK(AssetIdentifier::anchor("/sets/forest.usd", anchor).str() == "clio:/sets/forest.usd");
    CHECK(AssetIdentifier::anchor("clio:/other/x.usd", anchor).str() == "clio:/other/x.usd");
    CHECK_THROWS_AS(AssetIdentifier::anchor("../../../x.usd", anchor), IdentifierError);
}

TEST_CASE("snapshot pins are inherited, revision pins are not") {
    const auto atChange = AssetIdentifier::parse("clio:/props/crate/crate.usd?change=100");
    CHECK(AssetIdentifier::anchor("./geo.usdc", atChange).str() == "clio:/props/crate/geo.usdc?change=100");

    const auto atLabel = AssetIdentifier::parse("clio:/props/crate/crate.usd?label=approved");
    CHECK(*AssetIdentifier::anchor("./geo.usdc", atLabel).pin() == Pin::label("approved"));

    const auto atRev = AssetIdentifier::parse("clio:/props/crate/crate.usd?rev=4");
    CHECK_FALSE(AssetIdentifier::anchor("./geo.usdc", atRev).pin().has_value());

    // An explicit pin on the relative path wins.
    CHECK(AssetIdentifier::anchor("./geo.usdc?rev=2", atChange).str() == "clio:/props/crate/geo.usdc?rev=2");
}
