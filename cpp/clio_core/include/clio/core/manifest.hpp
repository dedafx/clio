#pragma once

#include <cstdint>
#include <map>
#include <string>

namespace clio::core {

/// One file of a manifest: the exact revision to load (design doc §10.8).
struct ManifestEntry {
    std::string path;     ///< relative to the depot root, '/' separators, not escaped
    long rev = 0;         ///< file revision
    long change = 0;      ///< change that made the revision (informational)
    std::string digest;   ///< the server's MD5 of the revision, upper-case hex
    std::int64_t size = 0; ///< the server's size of the revision, in bytes
    std::string type;     ///< Perforce file type, for example "binary+Fl"

    /// True for a text file type. `p4 print` writes those with the client's
    /// line endings, so a fetched copy is checked ignoring "\r".
    bool isText() const;

    /// False for types Perforce converts on the way to the client in a way
    /// that changes the bytes (utf16), where the digest cannot be checked.
    bool digestCheckable() const;
};

/// The exact revision of every file a stage can load, so that every machine
/// loads the same bytes (design doc §10.8). Read-only here: the manifest is
/// created and written by Python (`deda.clio.usd.Manifest`).
class Manifest {
public:
    /// Parse the JSON text of a manifest. Throws ConfigError if it is not a
    /// manifest this version understands.
    static Manifest parse(const std::string& text);

    /// Read and parse a manifest file. Throws ConfigError.
    static Manifest load(const std::string& path);

    const std::string& depot() const { return _depot; }
    const std::string& server() const { return _server; }
    const std::string& rootLayer() const { return _rootLayer; }
    const std::string& sourcePin() const { return _sourcePin; }
    const std::string& created() const { return _created; }
    const std::string& createdBy() const { return _createdBy; }
    const std::string& usdVersion() const { return _usdVersion; }
    const std::map<std::string, ManifestEntry>& entries() const { return _entries; }

    /// The entry for a path relative to the depot root, or null.
    const ManifestEntry* find(const std::string& relativePath) const;

    /// SHA-256 of the manifest's text: the job's version id. The same file
    /// always has the same id.
    const std::string& sha256() const { return _sha256; }

    /// Name of the manifest's folder in the version store: "manifest-" and
    /// the first 16 characters of the id.
    std::string folderName() const;

private:
    std::string _depot;
    std::string _server;
    std::string _rootLayer;
    std::string _sourcePin;
    std::string _created;
    std::string _createdBy;
    std::string _usdVersion;
    std::map<std::string, ManifestEntry> _entries;
    std::string _sha256;
};

} // namespace clio::core
