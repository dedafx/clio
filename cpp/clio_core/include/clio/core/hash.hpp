#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>

namespace clio::core {

/// MD5, the digest Perforce stores for every file revision. Used to check
/// what was fetched, not for security. Written out here so that clio_core
/// does not depend on the OpenSSL that P4API carries (design doc §8.4).
class Md5 {
public:
    Md5();
    void update(const void* data, std::size_t size);
    /// Upper-case hex, as Perforce prints it. The object cannot be used again.
    std::string finishHex();

private:
    void _block(const std::uint8_t* chunk);

    std::array<std::uint32_t, 4> _state;
    std::array<std::uint8_t, 64> _buffer{};
    std::uint64_t _bytes = 0;
};

/// SHA-256, for the id of a manifest.
class Sha256 {
public:
    Sha256();
    void update(const void* data, std::size_t size);
    /// Lower-case hex. The object cannot be used again.
    std::string finishHex();

private:
    void _block(const std::uint8_t* chunk);

    std::array<std::uint32_t, 8> _state;
    std::array<std::uint8_t, 64> _buffer{};
    std::uint64_t _bytes = 0;
};

std::string md5Hex(const std::string& data);
std::string sha256Hex(const std::string& data);

/// MD5 of a file's bytes. With `ignoreCarriageReturns`, every "\r\n" counts
/// as "\n": Perforce digests text files as stored on the server (LF), while
/// `p4 print` writes the client's line endings. Throws Error if the file
/// cannot be read.
std::string md5File(const std::filesystem::path& path, bool ignoreCarriageReturns = false);

} // namespace clio::core
