#include "clio/core/manifest.hpp"

#include "clio/core/error.hpp"
#include "clio/core/hash.hpp"

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <utility>
#include <vector>

namespace clio::core {

namespace {

// A small JSON reader for the manifest, which is plain JSON. It keeps object
// members in order and numbers as text, so large sizes stay exact.
struct Json {
    enum class Type { Null, Bool, Number, String, Array, Object } type = Type::Null;
    std::string text; ///< string value, or the number as written
    bool boolean = false;
    std::vector<Json> items;                           ///< array items
    std::vector<std::pair<std::string, Json>> members; ///< object members

    const Json* get(const std::string& key) const {
        for (const auto& [name, value] : members) {
            if (name == key) {
                return &value;
            }
        }
        return nullptr;
    }
};

class Reader {
public:
    explicit Reader(const std::string& text) : _text(text) {}

    Json parseDocument() {
        Json value = parseValue(0);
        skipSpace();
        if (_pos != _text.size()) {
            fail("unexpected text after the manifest");
        }
        return value;
    }

private:
    [[noreturn]] void fail(const std::string& what) const {
        throw ConfigError("Not a valid manifest (JSON error at character " + std::to_string(_pos) + "): " + what);
    }

    void skipSpace() {
        while (_pos < _text.size() && (_text[_pos] == ' ' || _text[_pos] == '\t' || _text[_pos] == '\n' ||
                                       _text[_pos] == '\r')) {
            ++_pos;
        }
    }

    char peek() {
        skipSpace();
        if (_pos >= _text.size()) {
            fail("the text ends too soon");
        }
        return _text[_pos];
    }

    void expect(char c) {
        if (peek() != c) {
            fail(std::string("expected '") + c + "'");
        }
        ++_pos;
    }

    bool consumeWord(const char* word) {
        const std::string w(word);
        if (_text.compare(_pos, w.size(), w) == 0) {
            _pos += w.size();
            return true;
        }
        return false;
    }

    Json parseValue(int depth) {
        if (depth > 64) {
            fail("nested too deeply");
        }
        Json value;
        const char c = peek();
        if (c == '{') {
            ++_pos;
            value.type = Json::Type::Object;
            if (peek() == '}') {
                ++_pos;
                return value;
            }
            for (;;) {
                if (peek() != '"') {
                    fail("expected a member name");
                }
                std::string name = parseString();
                expect(':');
                value.members.emplace_back(std::move(name), parseValue(depth + 1));
                if (peek() == ',') {
                    ++_pos;
                    continue;
                }
                expect('}');
                return value;
            }
        }
        if (c == '[') {
            ++_pos;
            value.type = Json::Type::Array;
            if (peek() == ']') {
                ++_pos;
                return value;
            }
            for (;;) {
                value.items.push_back(parseValue(depth + 1));
                if (peek() == ',') {
                    ++_pos;
                    continue;
                }
                expect(']');
                return value;
            }
        }
        if (c == '"') {
            value.type = Json::Type::String;
            value.text = parseString();
            return value;
        }
        if (consumeWord("true")) {
            value.type = Json::Type::Bool;
            value.boolean = true;
            return value;
        }
        if (consumeWord("false")) {
            value.type = Json::Type::Bool;
            return value;
        }
        if (consumeWord("null")) {
            return value;
        }
        const std::size_t start = _pos;
        while (_pos < _text.size() &&
               (std::isdigit(static_cast<unsigned char>(_text[_pos])) || _text[_pos] == '-' || _text[_pos] == '+' ||
                _text[_pos] == '.' || _text[_pos] == 'e' || _text[_pos] == 'E')) {
            ++_pos;
        }
        if (start == _pos) {
            fail("unexpected character");
        }
        value.type = Json::Type::Number;
        value.text = _text.substr(start, _pos - start);
        return value;
    }

    static void appendUtf8(std::string& out, unsigned long code) {
        if (code < 0x80) {
            out += static_cast<char>(code);
        } else if (code < 0x800) {
            out += static_cast<char>(0xC0 | (code >> 6));
            out += static_cast<char>(0x80 | (code & 0x3F));
        } else if (code < 0x10000) {
            out += static_cast<char>(0xE0 | (code >> 12));
            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (code & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (code >> 18));
            out += static_cast<char>(0x80 | ((code >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (code & 0x3F));
        }
    }

    unsigned long parseHex4() {
        if (_pos + 4 > _text.size()) {
            fail("a \\u escape is cut short");
        }
        unsigned long code = 0;
        for (int i = 0; i < 4; ++i) {
            const char h = _text[_pos++];
            code <<= 4;
            if (h >= '0' && h <= '9') code |= static_cast<unsigned long>(h - '0');
            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned long>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned long>(h - 'A' + 10);
            else fail("a \\u escape needs four hex digits");
        }
        return code;
    }

    std::string parseString() {
        ++_pos; // opening quote
        std::string out;
        for (;;) {
            if (_pos >= _text.size()) {
                fail("a string is not closed");
            }
            const char c = _text[_pos++];
            if (c == '"') {
                return out;
            }
            if (c != '\\') {
                out += c;
                continue;
            }
            if (_pos >= _text.size()) {
                fail("a string is not closed");
            }
            const char e = _text[_pos++];
            switch (e) {
            case '"': out += '"'; break;
            case '\\': out += '\\'; break;
            case '/': out += '/'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case 'n': out += '\n'; break;
            case 'r': out += '\r'; break;
            case 't': out += '\t'; break;
            case 'u': {
                unsigned long code = parseHex4();
                if (code >= 0xD800 && code < 0xDC00 && _text.compare(_pos, 2, "\\u") == 0) {
                    _pos += 2;
                    const unsigned long low = parseHex4();
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                }
                appendUtf8(out, code);
                break;
            }
            default: fail("unknown escape in a string");
            }
        }
    }

    const std::string& _text;
    std::size_t _pos = 0;
};

std::string stringField(const Json& object, const std::string& key, bool required, const std::string& where) {
    const Json* value = object.get(key);
    if (!value || value->type == Json::Type::Null) {
        if (required) {
            throw ConfigError("Not a valid manifest: " + where + " has no '" + key + "'");
        }
        return {};
    }
    if (value->type != Json::Type::String) {
        throw ConfigError("Not a valid manifest: '" + key + "' in " + where + " must be text");
    }
    return value->text;
}

std::int64_t numberField(const Json& object, const std::string& key, bool required, const std::string& where) {
    const Json* value = object.get(key);
    if (!value || value->type == Json::Type::Null) {
        if (required) {
            throw ConfigError("Not a valid manifest: " + where + " has no '" + key + "'");
        }
        return 0;
    }
    if (value->type != Json::Type::Number) {
        throw ConfigError("Not a valid manifest: '" + key + "' in " + where + " must be a number");
    }
    char* end = nullptr;
    const long long number = std::strtoll(value->text.c_str(), &end, 10);
    if (end == value->text.c_str() || *end != '\0') {
        throw ConfigError("Not a valid manifest: '" + key + "' in " + where + " must be a whole number");
    }
    return number;
}

bool validRelativePath(const std::string& path) {
    if (path.empty() || path.front() == '/' || path.find('\\') != std::string::npos ||
        path.find(':') != std::string::npos) {
        return false;
    }
    std::size_t start = 0;
    while (start <= path.size()) {
        std::size_t end = path.find('/', start);
        if (end == std::string::npos) {
            end = path.size();
        }
        const std::string part = path.substr(start, end - start);
        if (part.empty() || part == "." || part == "..") {
            return false;
        }
        start = end + 1;
    }
    return true;
}

} // namespace

bool ManifestEntry::isText() const {
    return type.rfind("text", 0) == 0 || type.rfind("unicode", 0) == 0 || type.rfind("utf8", 0) == 0;
}

bool ManifestEntry::digestCheckable() const {
    return type.rfind("utf16", 0) != 0 && !digest.empty();
}

Manifest Manifest::parse(const std::string& text) {
    const Json document = Reader(text).parseDocument();
    if (document.type != Json::Type::Object) {
        throw ConfigError("Not a valid manifest: expected a JSON object");
    }
    Manifest manifest;
    const auto version = numberField(document, "clio_manifest", true, "the manifest");
    if (version != 1) {
        throw ConfigError("This manifest has format " + std::to_string(version) +
                          "; this version of Clio reads format 1. Update Clio.");
    }
    manifest._depot = stringField(document, "depot", true, "the manifest");
    manifest._server = stringField(document, "server", false, "the manifest");
    manifest._rootLayer = stringField(document, "root_layer", false, "the manifest");
    manifest._sourcePin = stringField(document, "source_pin", false, "the manifest");
    manifest._created = stringField(document, "created", false, "the manifest");
    manifest._createdBy = stringField(document, "created_by", false, "the manifest");
    manifest._usdVersion = stringField(document, "usd_version", false, "the manifest");

    const Json* files = document.get("files");
    if (!files || files->type != Json::Type::Object) {
        throw ConfigError("Not a valid manifest: it has no 'files'");
    }
    for (const auto& [path, value] : files->members) {
        const std::string where = "the entry for " + path;
        if (!validRelativePath(path)) {
            throw ConfigError("Not a valid manifest: '" + path + "' is not a path inside the project");
        }
        if (value.type != Json::Type::Object) {
            throw ConfigError("Not a valid manifest: " + where + " must be an object");
        }
        ManifestEntry entry;
        entry.path = path;
        entry.rev = static_cast<long>(numberField(value, "rev", true, where));
        entry.change = static_cast<long>(numberField(value, "change", false, where));
        entry.digest = stringField(value, "digest", false, where);
        entry.size = numberField(value, "size", false, where);
        entry.type = stringField(value, "type", false, where);
        if (entry.rev < 1) {
            throw ConfigError("Not a valid manifest: " + where + " has a revision below 1");
        }
        for (char& c : entry.digest) {
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        }
        manifest._entries.emplace(path, std::move(entry));
    }
    manifest._sha256 = sha256Hex(text);
    return manifest;
}

Manifest Manifest::load(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw ConfigError("Could not read the manifest " + path);
    }
    std::ostringstream text;
    text << in.rdbuf();
    return parse(text.str());
}

const ManifestEntry* Manifest::find(const std::string& relativePath) const {
    const auto it = _entries.find(relativePath);
    return it == _entries.end() ? nullptr : &it->second;
}

std::string Manifest::folderName() const {
    return "manifest-" + _sha256.substr(0, 16);
}

} // namespace clio::core
