#pragma once

#include "clio/core/pin.hpp"

#include <optional>
#include <string>

namespace clio::core {

/// A `clio:` asset identifier (design doc §10.4 and §10.5).
///
/// Form: `clio:/<path relative to the project root>[?change=N|label=NAME|rev=N]`
///
///   clio:/props/crate/crate.usd
///   clio:/props/crate/crate.usd?change=18234
///   clio:/props/crate/crate.usd?label=approved
///   clio:/props/crate/geo/crate_geo.usdc?rev=3
///
/// The scheme is case-insensitive (as in USD's Ar). Paths are normalized:
/// backslashes become '/', "." and empty segments are removed, and ".." is
/// applied. A path may not escape the project root.
class AssetIdentifier {
public:
    static constexpr const char* scheme = "clio";

    /// True if `text` starts with the clio: scheme (case-insensitive).
    static bool isClioUri(const std::string& text);

    /// Parse and normalize. Throws IdentifierError or PinError.
    static AssetIdentifier parse(const std::string& text);

    /// From a '/'-separated path relative to the project root, as found on
    /// disk under the workspace root or the version store.
    static AssetIdentifier fromRelativePath(const std::string& relativePath,
                                            std::optional<Pin> pin = std::nullopt);

    /// Anchor `assetPath` to `anchor`, as USD does for relative paths in a
    /// layer. If `assetPath` is itself a clio: URI it is parsed on its own.
    /// Otherwise it is joined to the anchor's directory. A snapshot pin
    /// (change or label) on the anchor is inherited; a revision pin is not,
    /// because it only describes the anchor file itself.
    static AssetIdentifier anchor(const std::string& assetPath, const AssetIdentifier& anchor);

    /// Normalized path, always starting with '/'.
    const std::string& path() const { return _path; }

    /// The pin written in the identifier, if any. The resolver context
    /// supplies the pin otherwise.
    const std::optional<Pin>& pin() const { return _pin; }

    /// The path relative to the project root (no leading '/').
    std::string relativePath() const { return _path.substr(1); }

    /// Normalized text form, round-trips through parse().
    std::string str() const;

    bool operator==(const AssetIdentifier& other) const {
        return _path == other._path && _pin == other._pin;
    }
    bool operator!=(const AssetIdentifier& other) const { return !(*this == other); }

private:
    AssetIdentifier(std::string path, std::optional<Pin> pin)
        : _path(std::move(path)), _pin(std::move(pin)) {}

    std::string _path;
    std::optional<Pin> _pin;
};

} // namespace clio::core
