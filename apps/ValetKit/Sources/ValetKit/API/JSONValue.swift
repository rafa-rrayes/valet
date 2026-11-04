import Foundation

/// Any JSON value. Event data, tool inputs and outputs, and fields the apps don't model are kept as this, so
/// nothing a newer server or harness sends is lost or breaks decoding.
public enum JSONValue: Codable, Hashable, Sendable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    public subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    public var object: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    public var array: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    /// The text of a string; nil for anything else.
    public var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    public var double: Double? {
        if case .number(let n) = self { return n }
        return nil
    }

    public var int: Int? {
        if case .number(let n) = self, n.rounded() == n, abs(n) < 9.0e15 { return Int(n) }
        return nil
    }

    public var bool: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }

    public var isNull: Bool { self == .null }

    /// Null, "", {} or []: nothing worth showing (app.js's isEmpty).
    public var isEmpty: Bool {
        switch self {
        case .null: return true
        case .string(let s): return s.isEmpty
        case .object(let o): return o.isEmpty
        case .array(let a): return a.isEmpty
        default: return false
        }
    }

    /// A scalar as text: strings as they are, whole numbers without ".0", booleans as true/false.
    public var text: String? {
        switch self {
        case .string(let s): return s
        case .number(let n): return n.rounded() == n && abs(n) < 9.0e15 ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        default: return nil
        }
    }

    /// Strings as they are, anything else as indented JSON (a tool's input and output).
    public var display: String {
        if case .string(let s) = self { return s }
        return pretty
    }

    public var pretty: String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return (try? enc.encode(self)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }

    /// Decodes a Decodable from this value (a typed view of part of an event's data).
    public func decode<T: Decodable>(_ type: T.Type = T.self) -> T? {
        guard let data = try? JSONEncoder().encode(self) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByBooleanLiteral,
    ExpressibleByDictionaryLiteral, ExpressibleByArrayLiteral, ExpressibleByNilLiteral, ExpressibleByFloatLiteral {
    public init(stringLiteral value: String) { self = .string(value) }
    public init(integerLiteral value: Int) { self = .number(Double(value)) }
    public init(floatLiteral value: Double) { self = .number(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(dictionaryLiteral elements: (String, JSONValue)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, b in b }))
    }
    public init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
    public init(nilLiteral: ()) { self = .null }
}

// MARK: lenient decoding

/// A coding key from any string, for reading an object's keys one by one.
struct AnyKey: CodingKey, Hashable {
    let stringValue: String
    var intValue: Int? { nil }
    init(_ s: String) { stringValue = s }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}

extension KeyedDecodingContainer where Key == AnyKey {
    /// The value at `key` if it's there and has the right type; nil otherwise (never throws).
    func get<T: Decodable>(_ key: String, as type: T.Type = T.self) -> T? {
        (try? decodeIfPresent(T.self, forKey: AnyKey(key))) ?? nil
    }

    /// A list whose elements that don't decode are dropped, so one odd element doesn't lose the others.
    func list<T: Decodable>(_ key: String, of type: T.Type = T.self) -> [T] {
        get(key, as: [Lenient<T>].self)?.compactMap(\.value) ?? []
    }

    /// Every key that isn't in `known`, as JSON: what a newer server added.
    func rest(excluding known: Set<String>) -> [String: JSONValue] {
        var out: [String: JSONValue] = [:]
        for key in allKeys where !known.contains(key.stringValue) {
            if let v = try? decode(JSONValue.self, forKey: key) { out[key.stringValue] = v }
        }
        return out
    }
}

/// One element of a lenient list: its value, or nil if it didn't decode.
struct Lenient<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

/// A string from a closed set the server may grow (statuses, kinds): unknown values are kept, never an error.
public protocol OpenEnum: RawRepresentable, Codable, Hashable, Sendable, ExpressibleByStringLiteral
where RawValue == String {
    init(rawValue: String)
}

extension OpenEnum {
    public init(stringLiteral value: String) { self.init(rawValue: value) }
    /// A string, or a decoding error (so a lenient read of a mistyped value comes out nil, then the default).
    public init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(rawValue)
    }
}

/// An id the server sends as a number or a string (devices): kept as its text.
public struct FlexibleID: Codable, Hashable, Sendable, CustomStringConvertible {
    public let value: String
    public init(_ value: String) { self.value = value }
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { value = s }
        else if let i = try? c.decode(Int.self) { value = String(i) }
        else { value = JSONValue.number(try c.decode(Double.self)).text ?? "" }
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(value)
    }
    public var description: String { value }
}
