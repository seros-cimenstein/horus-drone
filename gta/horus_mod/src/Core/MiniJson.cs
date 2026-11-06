using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace HorusGta
{
    /// <summary>
    /// The JSON the survey needs (.NET Framework 4.8 has no JSON reader): <see cref="Parse"/> reads a
    /// whole document into Dictionary&lt;string, object&gt; / List&lt;object&gt; / double / string /
    /// bool / null and throws <see cref="FormatException"/> on anything else; <see cref="Quote"/>
    /// and <see cref="Number"/> write values.
    /// </summary>
    public static class MiniJson
    {
        public static object Parse(string text)
        {
            text = text ?? "";
            int i = 0;
            object value = Value(text, ref i, 0);
            Skip(text, ref i);
            if (i != text.Length) throw new FormatException($"unexpected '{text[i]}' at {i} after the value");
            return value;
        }

        /// <summary>A JSON string literal.</summary>
        public static string Quote(string s)
        {
            var b = new StringBuilder(s.Length + 2).Append('"');
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') b.Append('\\').Append(c);
                else if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                else b.Append(c);
            }
            return b.Append('"').ToString();
        }

        /// <summary>A JSON number (round-trip precision), or null for NaN/infinity.</summary>
        public static string Number(double v) =>
            double.IsNaN(v) || double.IsInfinity(v) ? "null" : v.ToString("R", CultureInfo.InvariantCulture);

        const int MaxDepth = 32;

        static object Value(string s, ref int i, int depth)
        {
            if (depth > MaxDepth) throw new FormatException("nested too deeply");
            Skip(s, ref i);
            if (i >= s.Length) throw new FormatException("unexpected end");
            char c = s[i];
            if (c == '{')
            {
                var o = new Dictionary<string, object>(StringComparer.Ordinal);
                i++;
                Skip(s, ref i);
                if (At(s, i, '}')) { i++; return o; }
                for (;;)
                {
                    Skip(s, ref i);
                    if (!At(s, i, '"')) throw new FormatException($"expected a key at {i}");
                    string key = String(s, ref i);
                    Skip(s, ref i);
                    if (!At(s, i, ':')) throw new FormatException($"expected ':' at {i}");
                    i++;
                    if (o.ContainsKey(key)) throw new FormatException($"duplicate key \"{key}\"");
                    o[key] = Value(s, ref i, depth + 1);
                    Skip(s, ref i);
                    if (At(s, i, ',')) { i++; continue; }
                    if (At(s, i, '}')) { i++; return o; }
                    throw new FormatException($"expected ',' or '}}' at {i}");
                }
            }
            if (c == '[')
            {
                var a = new List<object>();
                i++;
                Skip(s, ref i);
                if (At(s, i, ']')) { i++; return a; }
                for (;;)
                {
                    a.Add(Value(s, ref i, depth + 1));
                    Skip(s, ref i);
                    if (At(s, i, ',')) { i++; continue; }
                    if (At(s, i, ']')) { i++; return a; }
                    throw new FormatException($"expected ',' or ']' at {i}");
                }
            }
            if (c == '"') return String(s, ref i);
            if (Word(s, ref i, "true")) return true;
            if (Word(s, ref i, "false")) return false;
            if (Word(s, ref i, "null")) return null;
            int start = i;
            if (At(s, i, '-')) i++;
            while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+' || s[i] == '-')) i++;
            if (i == start || !double.TryParse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture, out double d)
                || double.IsInfinity(d))
                throw new FormatException($"unexpected '{c}' at {start}");
            return d;
        }

        static string String(string s, ref int i)
        {
            var b = new StringBuilder();
            i++;
            while (i < s.Length)
            {
                char c = s[i++];
                if (c == '"') return b.ToString();
                if (c < 0x20) throw new FormatException("control character in a string");
                if (c != '\\') { b.Append(c); continue; }
                if (i >= s.Length) break;
                char e = s[i++];
                switch (e)
                {
                    case '"': case '\\': case '/': b.Append(e); break;
                    case 'b': b.Append('\b'); break;
                    case 'f': b.Append('\f'); break;
                    case 'n': b.Append('\n'); break;
                    case 'r': b.Append('\r'); break;
                    case 't': b.Append('\t'); break;
                    case 'u':
                        if (i + 4 > s.Length || !int.TryParse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out int u))
                            throw new FormatException("bad \\u escape");
                        b.Append((char)u);
                        i += 4;
                        break;
                    default: throw new FormatException($"bad escape \\{e}");
                }
            }
            throw new FormatException("unterminated string");
        }

        static bool Word(string s, ref int i, string w)
        {
            if (string.CompareOrdinal(s, i, w, 0, w.Length) != 0) return false;
            i += w.Length;
            return true;
        }

        static bool At(string s, int i, char c) => i < s.Length && s[i] == c;

        static void Skip(string s, ref int i)
        {
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n' || s[i] == '\uFEFF')) i++;
        }
    }
}
