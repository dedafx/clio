#include "clio/core/hash.hpp"

#include "clio/core/error.hpp"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <vector>

namespace clio::core {

namespace {

inline std::uint32_t rotl(std::uint32_t x, int n) { return (x << n) | (x >> (32 - n)); }
inline std::uint32_t rotr(std::uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

std::string toHex(const std::uint8_t* bytes, std::size_t count, bool upper) {
    const char* digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    std::string out;
    out.reserve(count * 2);
    for (std::size_t i = 0; i < count; ++i) {
        out += digits[bytes[i] >> 4];
        out += digits[bytes[i] & 0xF];
    }
    return out;
}

// --- MD5 (RFC 1321) ---------------------------------------------------------

constexpr std::uint32_t md5K[64] = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391};

constexpr int md5S[64] = {7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
                          5, 9,  14, 20, 5, 9,  14, 20, 5, 9,  14, 20, 5, 9,  14, 20,
                          4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
                          6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21};

// --- SHA-256 (FIPS 180-4) -----------------------------------------------------

constexpr std::uint32_t shaK[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

// Feeds `data` through `block` in 64-byte chunks, keeping a partial chunk in `buffer`.
template <class Block>
void feed(const std::uint8_t* data, std::size_t size, std::array<std::uint8_t, 64>& buffer,
          std::uint64_t& total, Block block) {
    std::size_t used = static_cast<std::size_t>(total % 64);
    total += size;
    if (used) {
        const std::size_t take = std::min<std::size_t>(64 - used, size);
        std::memcpy(buffer.data() + used, data, take);
        data += take;
        size -= take;
        used += take;
        if (used < 64) {
            return;
        }
        block(buffer.data());
    }
    while (size >= 64) {
        block(data);
        data += 64;
        size -= 64;
    }
    std::memcpy(buffer.data(), data, size);
}

// Pads the final chunk (0x80, zeros, bit length) and feeds it.
template <class Block>
void pad(std::array<std::uint8_t, 64>& buffer, std::uint64_t total, bool bigEndianLength, Block block) {
    std::uint8_t tail[128] = {0};
    const std::size_t used = static_cast<std::size_t>(total % 64);
    std::memcpy(tail, buffer.data(), used);
    tail[used] = 0x80;
    const std::size_t blocks = used < 56 ? 1 : 2;
    const std::uint64_t bits = total * 8;
    for (int i = 0; i < 8; ++i) {
        const int shift = bigEndianLength ? (7 - i) * 8 : i * 8;
        tail[blocks * 64 - 8 + i] = static_cast<std::uint8_t>(bits >> shift);
    }
    for (std::size_t b = 0; b < blocks; ++b) {
        block(tail + b * 64);
    }
}

} // namespace

Md5::Md5() : _state{0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476} {}

void Md5::_block(const std::uint8_t* chunk) {
    std::uint32_t m[16];
    for (int i = 0; i < 16; ++i) {
        m[i] = static_cast<std::uint32_t>(chunk[i * 4]) | static_cast<std::uint32_t>(chunk[i * 4 + 1]) << 8 |
               static_cast<std::uint32_t>(chunk[i * 4 + 2]) << 16 | static_cast<std::uint32_t>(chunk[i * 4 + 3]) << 24;
    }
    std::uint32_t a = _state[0], b = _state[1], c = _state[2], d = _state[3];
    for (int i = 0; i < 64; ++i) {
        std::uint32_t f;
        int g;
        if (i < 16) {
            f = (b & c) | (~b & d);
            g = i;
        } else if (i < 32) {
            f = (d & b) | (~d & c);
            g = (5 * i + 1) % 16;
        } else if (i < 48) {
            f = b ^ c ^ d;
            g = (3 * i + 5) % 16;
        } else {
            f = c ^ (b | ~d);
            g = (7 * i) % 16;
        }
        f = f + a + md5K[i] + m[g];
        a = d;
        d = c;
        c = b;
        b = b + rotl(f, md5S[i]);
    }
    _state[0] += a;
    _state[1] += b;
    _state[2] += c;
    _state[3] += d;
}

void Md5::update(const void* data, std::size_t size) {
    feed(static_cast<const std::uint8_t*>(data), size, _buffer, _bytes,
         [this](const std::uint8_t* chunk) { _block(chunk); });
}

std::string Md5::finishHex() {
    pad(_buffer, _bytes, /*bigEndianLength=*/false, [this](const std::uint8_t* chunk) { _block(chunk); });
    std::uint8_t out[16];
    for (int i = 0; i < 4; ++i) {
        for (int j = 0; j < 4; ++j) {
            out[i * 4 + j] = static_cast<std::uint8_t>(_state[i] >> (j * 8));
        }
    }
    return toHex(out, 16, /*upper=*/true);
}

Sha256::Sha256()
    : _state{0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19} {}

void Sha256::_block(const std::uint8_t* chunk) {
    std::uint32_t w[64];
    for (int i = 0; i < 16; ++i) {
        w[i] = static_cast<std::uint32_t>(chunk[i * 4]) << 24 | static_cast<std::uint32_t>(chunk[i * 4 + 1]) << 16 |
               static_cast<std::uint32_t>(chunk[i * 4 + 2]) << 8 | static_cast<std::uint32_t>(chunk[i * 4 + 3]);
    }
    for (int i = 16; i < 64; ++i) {
        const std::uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const std::uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    std::uint32_t a = _state[0], b = _state[1], c = _state[2], d = _state[3];
    std::uint32_t e = _state[4], f = _state[5], g = _state[6], h = _state[7];
    for (int i = 0; i < 64; ++i) {
        const std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const std::uint32_t ch = (e & f) ^ (~e & g);
        const std::uint32_t t1 = h + s1 + ch + shaK[i] + w[i];
        const std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const std::uint32_t t2 = s0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    _state[0] += a;
    _state[1] += b;
    _state[2] += c;
    _state[3] += d;
    _state[4] += e;
    _state[5] += f;
    _state[6] += g;
    _state[7] += h;
}

void Sha256::update(const void* data, std::size_t size) {
    feed(static_cast<const std::uint8_t*>(data), size, _buffer, _bytes,
         [this](const std::uint8_t* chunk) { _block(chunk); });
}

std::string Sha256::finishHex() {
    pad(_buffer, _bytes, /*bigEndianLength=*/true, [this](const std::uint8_t* chunk) { _block(chunk); });
    std::uint8_t out[32];
    for (int i = 0; i < 8; ++i) {
        for (int j = 0; j < 4; ++j) {
            out[i * 4 + j] = static_cast<std::uint8_t>(_state[i] >> (24 - j * 8));
        }
    }
    return toHex(out, 32, /*upper=*/false);
}

std::string md5Hex(const std::string& data) {
    Md5 md5;
    md5.update(data.data(), data.size());
    return md5.finishHex();
}

std::string sha256Hex(const std::string& data) {
    Sha256 sha;
    sha.update(data.data(), data.size());
    return sha.finishHex();
}

std::string md5File(const std::filesystem::path& path, bool ignoreCarriageReturns) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw Error("Could not read " + path.string());
    }
    Md5 md5;
    std::vector<char> buffer(1 << 20);
    bool pendingCr = false; // a '\r' at the end of the last chunk, not yet hashed
    while (in) {
        in.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::size_t got = static_cast<std::size_t>(in.gcount());
        if (!got) {
            break;
        }
        if (!ignoreCarriageReturns) {
            md5.update(buffer.data(), got);
            continue;
        }
        std::string kept;
        kept.reserve(got + 1);
        if (pendingCr) {
            if (buffer[0] != '\n') {
                kept += '\r';
            }
            pendingCr = false;
        }
        for (std::size_t i = 0; i < got; ++i) {
            if (buffer[i] == '\r') {
                if (i + 1 < got) {
                    if (buffer[i + 1] != '\n') {
                        kept += '\r';
                    }
                } else {
                    pendingCr = true;
                }
            } else {
                kept += buffer[i];
            }
        }
        md5.update(kept.data(), kept.size());
    }
    if (pendingCr) {
        md5.update("\r", 1);
    }
    return md5.finishHex();
}

} // namespace clio::core
