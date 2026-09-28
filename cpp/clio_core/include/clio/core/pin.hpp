#pragma once

#include <cstdint>
#include <string>

namespace clio::core {

/// Which version of an asset to use (design doc §10.4).
///
/// Text forms accepted by Pin::parse and produced by Pin::str:
///   "have"     the file on disk; a file not on disk yet is synced (default)
///   "latest"   head revision on the branch, synced even if a file is on disk
///   "@18234"   snapshot at a submitted change number
///   "@approved" snapshot at a label
///   "#12"      a single file's revision
class Pin {
public:
    enum class Kind { Latest, Have, Change, Label, Revision };

    Pin() = default;

    static Pin latest() { return Pin(Kind::Latest, 0, {}); }
    static Pin have() { return Pin(Kind::Have, 0, {}); }
    static Pin change(std::uint64_t change);
    static Pin label(const std::string& label);
    static Pin revision(std::uint64_t revision);

    /// Parse the text form. Throws PinError.
    static Pin parse(const std::string& text);

    Kind kind() const { return _kind; }
    std::uint64_t number() const { return _number; }
    const std::string& labelName() const { return _label; }

    /// A snapshot pin (change or label) applies to a whole set of files and
    /// is inherited by relative paths. Latest, have and revision pins are not
    /// snapshots.
    bool isSnapshot() const { return _kind == Kind::Change || _kind == Kind::Label; }

    /// True for pins whose content never changes (change, label, revision).
    /// Labels can technically be moved by admins; see design doc §9.2.
    bool isHistorical() const { return isSnapshot() || _kind == Kind::Revision; }

    /// Text form, round-trips through parse().
    std::string str() const;

    /// Perforce revision specifier to append to a depot path:
    /// "#head", "#have", "@18234", "@approved", "#12".
    std::string p4RevSpec() const;

    bool operator==(const Pin& other) const {
        return _kind == other._kind && _number == other._number && _label == other._label;
    }
    bool operator!=(const Pin& other) const { return !(*this == other); }
    bool operator<(const Pin& other) const;

private:
    Pin(Kind kind, std::uint64_t number, std::string label)
        : _kind(kind), _number(number), _label(std::move(label)) {}

    Kind _kind = Kind::Latest;
    std::uint64_t _number = 0;
    std::string _label;
};

} // namespace clio::core
