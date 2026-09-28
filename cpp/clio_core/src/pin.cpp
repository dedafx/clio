#include "clio/core/pin.hpp"

#include "clio/core/error.hpp"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <tuple>

namespace clio::core {

namespace {

std::uint64_t parsePositive(const std::string& digits, const std::string& original) {
    std::uint64_t value = 0;
    const char* first = digits.data();
    const char* last = digits.data() + digits.size();
    auto [ptr, ec] = std::from_chars(first, last, value);
    if (digits.empty() || ec != std::errc() || ptr != last || value == 0) {
        throw PinError("Invalid version pin '" + original +
                       "': expected a positive number after '@' or '#'");
    }
    return value;
}

bool isAllDigits(const std::string& s) {
    return !s.empty() &&
           std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c); });
}

// Perforce label names may not be purely numeric and may not contain
// whitespace, revision characters or wildcards.
void validateLabel(const std::string& label, const std::string& original) {
    if (label.empty() || isAllDigits(label)) {
        throw PinError("Invalid label in version pin '" + original + "'");
    }
    for (unsigned char c : label) {
        if (std::isspace(c) || c == '@' || c == '#' || c == '%' || c == '*' || c == ',' ||
            c == '/' || c == '?' || c == ';' || c == '=' || c == '&') {
            throw PinError("Invalid character in label of version pin '" + original + "'");
        }
    }
    if (label.find("...") != std::string::npos) {
        throw PinError("Invalid label in version pin '" + original + "'");
    }
}

} // namespace

Pin Pin::change(std::uint64_t change) {
    if (change == 0) {
        throw PinError("Change number must be positive");
    }
    return Pin(Kind::Change, change, {});
}

Pin Pin::label(const std::string& label) {
    validateLabel(label, "@" + label);
    return Pin(Kind::Label, 0, label);
}

Pin Pin::revision(std::uint64_t revision) {
    if (revision == 0) {
        throw PinError("Revision number must be positive");
    }
    return Pin(Kind::Revision, revision, {});
}

Pin Pin::parse(const std::string& text) {
    if (text == "latest" || text == "#head") {
        return latest();
    }
    if (text == "have" || text == "#have") {
        return have();
    }
    if (text.size() > 1 && text[0] == '@') {
        const std::string rest = text.substr(1);
        if (isAllDigits(rest)) {
            return Pin(Kind::Change, parsePositive(rest, text), {});
        }
        validateLabel(rest, text);
        return Pin(Kind::Label, 0, rest);
    }
    if (text.size() > 1 && text[0] == '#') {
        return Pin(Kind::Revision, parsePositive(text.substr(1), text), {});
    }
    throw PinError("Invalid version pin '" + text +
                   "': expected latest, have, @<change>, @<label> or #<revision>");
}

std::string Pin::str() const {
    switch (_kind) {
    case Kind::Latest:
        return "latest";
    case Kind::Have:
        return "have";
    case Kind::Change:
        return "@" + std::to_string(_number);
    case Kind::Label:
        return "@" + _label;
    case Kind::Revision:
        return "#" + std::to_string(_number);
    }
    return "latest";
}

std::string Pin::p4RevSpec() const {
    switch (_kind) {
    case Kind::Latest:
        return "#head";
    case Kind::Have:
        return "#have";
    default:
        return str();
    }
}

bool Pin::operator<(const Pin& other) const {
    return std::tie(_kind, _number, _label) < std::tie(other._kind, other._number, other._label);
}

} // namespace clio::core
