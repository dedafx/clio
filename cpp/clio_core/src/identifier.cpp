#include "clio/core/identifier.hpp"

#include "clio/core/error.hpp"

#include <algorithm>
#include <cctype>
#include <vector>

namespace clio::core {

namespace {

constexpr std::size_t schemeLength = 5; // "clio:"

bool startsWithNoCase(const std::string& text, const std::string& prefix) {
    return text.size() >= prefix.size() &&
           std::equal(prefix.begin(), prefix.end(), text.begin(), [](char a, char b) {
               return std::tolower(static_cast<unsigned char>(a)) ==
                      std::tolower(static_cast<unsigned char>(b));
           });
}

// Normalize a '/'-separated path. `path` may be absolute or relative; the
// result is always absolute within the project root. Throws if ".." escapes.
std::string normalizePath(std::string path, const std::string& original) {
    std::replace(path.begin(), path.end(), '\\', '/');

    std::vector<std::string> parts;
    std::size_t start = 0;
    while (start <= path.size()) {
        std::size_t end = path.find('/', start);
        if (end == std::string::npos) {
            end = path.size();
        }
        std::string part = path.substr(start, end - start);
        if (part == "..") {
            if (parts.empty()) {
                throw IdentifierError("Asset path '" + original + "' goes above the project root");
            }
            parts.pop_back();
        } else if (!part.empty() && part != ".") {
            parts.push_back(std::move(part));
        }
        start = end + 1;
    }
    if (parts.empty()) {
        throw IdentifierError("Asset path '" + original + "' does not name a file");
    }

    std::string result;
    for (const auto& part : parts) {
        result += '/';
        result += part;
    }
    return result;
}

std::optional<Pin> parseQuery(const std::string& query, const std::string& original) {
    if (query.empty()) {
        return std::nullopt;
    }
    const std::size_t eq = query.find('=');
    if (eq == std::string::npos || query.find('&') != std::string::npos) {
        throw IdentifierError("Invalid query in '" + original +
                              "': expected one of ?change=N, ?label=NAME, ?rev=N");
    }
    const std::string key = query.substr(0, eq);
    const std::string value = query.substr(eq + 1);
    if (key == "change") {
        return Pin::parse("@" + value);
    }
    if (key == "label") {
        return Pin::label(value);
    }
    if (key == "rev") {
        return Pin::parse("#" + value);
    }
    throw IdentifierError("Unknown query key '" + key + "' in '" + original + "'");
}

std::string pinQuery(const Pin& pin) {
    switch (pin.kind()) {
    case Pin::Kind::Change:
        return "?change=" + std::to_string(pin.number());
    case Pin::Kind::Label:
        return "?label=" + pin.labelName();
    case Pin::Kind::Revision:
        return "?rev=" + std::to_string(pin.number());
    case Pin::Kind::Latest:
    case Pin::Kind::Have:
        break;
    }
    return {};
}

} // namespace

bool AssetIdentifier::isClioUri(const std::string& text) {
    return startsWithNoCase(text, "clio:");
}

AssetIdentifier AssetIdentifier::parse(const std::string& text) {
    if (!isClioUri(text)) {
        throw IdentifierError("'" + text + "' is not a clio: identifier");
    }
    std::string rest = text.substr(schemeLength);

    std::string query;
    if (const std::size_t q = rest.find('?'); q != std::string::npos) {
        query = rest.substr(q + 1);
        rest.erase(q);
    }
    // Accept "clio:/path" and "clio:///path"; reject an authority such as
    // "clio://server/path", which is reserved for future use.
    if (rest.rfind("//", 0) == 0 && rest.rfind("///", 0) != 0) {
        throw IdentifierError("'" + text + "': clio://<authority>/ is not supported, use clio:/path");
    }
    if (rest.empty() || rest[0] != '/') {
        throw IdentifierError("'" + text + "': expected an absolute path after clio:, as in clio:/props/crate.usd");
    }
    // Pins like "latest" and "have" belong in the resolver context, not the path.
    return AssetIdentifier(normalizePath(rest, text), parseQuery(query, text));
}

AssetIdentifier AssetIdentifier::fromRelativePath(const std::string& relativePath,
                                                  std::optional<Pin> pin) {
    return AssetIdentifier(normalizePath(relativePath, relativePath), std::move(pin));
}

AssetIdentifier AssetIdentifier::anchor(const std::string& assetPath, const AssetIdentifier& anchor) {
    if (isClioUri(assetPath)) {
        return parse(assetPath);
    }

    std::string path = assetPath;
    std::string query;
    if (const std::size_t q = path.find('?'); q != std::string::npos) {
        query = path.substr(q + 1);
        path.erase(q);
    }

    std::string joined;
    if (!path.empty() && (path[0] == '/' || path[0] == '\\')) {
        // Root-relative within the project.
        joined = path;
    } else {
        const std::string& base = anchor.path();
        joined = base.substr(0, base.rfind('/') + 1) + path;
    }

    std::optional<Pin> pin = parseQuery(query, assetPath);
    if (!pin && anchor.pin() && anchor.pin()->isSnapshot()) {
        pin = anchor.pin();
    }
    return AssetIdentifier(normalizePath(joined, assetPath), std::move(pin));
}

std::string AssetIdentifier::str() const {
    std::string result = "clio:" + _path;
    if (_pin) {
        result += pinQuery(*_pin);
    }
    return result;
}

} // namespace clio::core
