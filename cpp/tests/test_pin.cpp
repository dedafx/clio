#include "clio/core/error.hpp"
#include "clio/core/pin.hpp"

#include <doctest/doctest.h>

using clio::core::Pin;
using clio::core::PinError;

TEST_CASE("pins parse and round-trip") {
    for (const char* text : {"latest", "have", "@18234", "@approved", "@delivery_0412", "#12"}) {
        CAPTURE(text);
        CHECK(Pin::parse(text).str() == text);
    }
    CHECK(Pin::parse("#head") == Pin::latest());
    CHECK(Pin::parse("#have") == Pin::have());
}

TEST_CASE("pin kinds and revision specifiers") {
    CHECK(Pin::parse("@18234").kind() == Pin::Kind::Change);
    CHECK(Pin::parse("@18234").number() == 18234);
    CHECK(Pin::parse("@approved").kind() == Pin::Kind::Label);
    CHECK(Pin::parse("@approved").labelName() == "approved");
    CHECK(Pin::parse("#3").kind() == Pin::Kind::Revision);

    CHECK(Pin::latest().p4RevSpec() == "#head");
    CHECK(Pin::have().p4RevSpec() == "#have");
    CHECK(Pin::change(5).p4RevSpec() == "@5");
    CHECK(Pin::label("approved").p4RevSpec() == "@approved");
    CHECK(Pin::revision(2).p4RevSpec() == "#2");
}

TEST_CASE("snapshot and historical pins") {
    CHECK(Pin::change(1).isSnapshot());
    CHECK(Pin::label("x1").isSnapshot());
    CHECK_FALSE(Pin::revision(1).isSnapshot());
    CHECK(Pin::revision(1).isHistorical());
    CHECK_FALSE(Pin::latest().isHistorical());
    CHECK_FALSE(Pin::have().isHistorical());
}

TEST_CASE("invalid pins are rejected") {
    for (const char* text : {"", "@", "#", "@0", "#0", "#abc", "@has space", "@a*b", "@...",
                             "head", "@1.5x@", "#-1"}) {
        CAPTURE(text);
        CHECK_THROWS_AS(Pin::parse(text), PinError);
    }
}
