#include "clio/core/error.hpp"
#include "clio/core/settings.hpp"

#include <doctest/doctest.h>

using clio::core::ConfigError;
using clio::core::Pin;
using clio::core::Policy;
using clio::core::Settings;

TEST_CASE("settings parse from the context string") {
    const auto s = Settings::parse(
        " depot=//imagine/main ; root=/work/imagine; store=/cache/v; client=sam_ws;"
        "port=ssl:perforce:1666; user=sam; tickets=/home/sam/.p4tickets; pin=@18234; policy=verify; timeout=30 ");
    CHECK(s.depotRoot == "//imagine/main");
    CHECK(s.workspaceRoot == "/work/imagine");
    CHECK(s.versionStore == "/cache/v");
    CHECK(s.connection.client == "sam_ws");
    CHECK(s.connection.port == "ssl:perforce:1666");
    CHECK(s.connection.user == "sam");
    CHECK(s.connection.ticketFile == "/home/sam/.p4tickets");
    CHECK(s.pin == Pin::change(18234));
    CHECK(s.policy == Policy::Verify);
    CHECK(s.connection.commandTimeout.count() == 30);
}

TEST_CASE("the canonical form round-trips and ignores order") {
    const auto a = Settings::parse("root=/w;depot=//d/main;store=/s;pin=@approved");
    const auto b = Settings::parse("pin=@approved;store=/s;depot=//d/main;root=/w");
    CHECK(a.str() == b.str());
    CHECK(a == b);
    CHECK(Settings::parse(a.str()) == a);
    CHECK(a.str() == "depot=//d/main;root=/w;store=/s;pin=@approved");
}

TEST_CASE("invalid settings are rejected") {
    for (const char* text : {"", "root=/w", "depot=//d", "depot=d/main;root=/w",
                             "depot=//d/main/;root=/w", "depot=//d/...;root=/w",
                             "depot=//d;root=/w;bogus=1", "depot=//d;root=/w;policy=maybe",
                             "depot=//d;root=/w;timeout=-1", "depot=//d;root=/w;pin=#3",
                             "depot=//d;root=/w;timeout=10seconds", "depot=//d;root=/w;connect_timeout=5s",
                             "depot=//d;root=/w;timeout=", "depot=//d;root=/w;timeout= 7x",
                             "depot=//d;depot=//e;root=/w", "depot=//d;root"}) {
        CAPTURE(text);
        CHECK_THROWS_AS(Settings::parse(text), clio::core::Error);
    }
}

TEST_CASE("the default pin is have") {
    const auto s = Settings::parse("depot=//d/main;root=/w;store=/s");
    CHECK(s.pin == Pin::have());
    CHECK(s.str() == "depot=//d/main;root=/w;store=/s");
    CHECK(Settings::parse("depot=//d/main;root=/w;store=/s;pin=latest").str() ==
          "depot=//d/main;root=/w;store=/s;pin=latest");
}

TEST_CASE("version store defaults to the user cache") {
    const auto s = Settings::parse("depot=//d/main;root=/w");
    CHECK_FALSE(s.versionStore.empty());
}
