using System.Text.Encodings.Web;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Encoder JSON untuk kawat OBS: hanya escape yang wajib RFC
/// (<c>"</c>, <c>\</c>, kontrol U+0000–U+001F) plus surrogate yatim.
///
/// net6 <see cref="JavaScriptEncoder.Create"/> / <c>UnicodeRanges.All</c>
/// dan <see cref="JavaScriptEncoder.UnsafeRelaxedJsonEscaping"/> tetap
/// menulis pasangan surrogat sebagai <c>\uXXXX\uXXXX</c> (~12 ASCII per
/// code point 4-byte). Itu mematahkan bukti Bound UTF-8 = ukuran NDJSON
/// dan bisa melewati MAX_LINE_BYTES. Loopback NDJSON, bukan HTML.
/// </summary>
public sealed class Utf8WireJsonEncoder : JavaScriptEncoder
{
    public static Utf8WireJsonEncoder Instance { get; } = new();

    Utf8WireJsonEncoder()
    {
    }

    public override int MaxOutputCharactersPerInputCharacter => 6;

    public override bool WillEncode(int unicodeScalar) => MustEscape(unicodeScalar);

    public override unsafe int FindFirstCharacterToEncode(char* text, int textLength)
    {
        for (int i = 0; i < textLength; i++)
        {
            char c = text[i];
            if (char.IsHighSurrogate(c) && i + 1 < textLength && char.IsLowSurrogate(text[i + 1]))
            {
                i++;
                continue;
            }

            if (MustEscape(c) || char.IsSurrogate(c))
                return i;
        }

        return -1;
    }

    public override unsafe bool TryEncodeUnicodeScalar(
        int unicodeScalar,
        char* destination,
        int destinationLength,
        out int charsWritten
    )
    {
        if (!MustEscape(unicodeScalar))
        {
            if (unicodeScalar <= char.MaxValue)
            {
                if (destinationLength < 1)
                {
                    charsWritten = 0;
                    return false;
                }

                destination[0] = (char)unicodeScalar;
                charsWritten = 1;
                return true;
            }

            if (destinationLength < 2)
            {
                charsWritten = 0;
                return false;
            }

            int x = unicodeScalar - 0x10000;
            destination[0] = (char)((x >> 10) + 0xD800);
            destination[1] = (char)((x & 0x3FF) + 0xDC00);
            charsWritten = 2;
            return true;
        }

        if (unicodeScalar is '"' or '\\')
        {
            if (destinationLength < 2)
            {
                charsWritten = 0;
                return false;
            }

            destination[0] = '\\';
            destination[1] = (char)unicodeScalar;
            charsWritten = 2;
            return true;
        }

        if (destinationLength < 6)
        {
            charsWritten = 0;
            return false;
        }

        int value = unicodeScalar;
        destination[0] = '\\';
        destination[1] = 'u';
        destination[2] = HexDigit((value >> 12) & 0xF);
        destination[3] = HexDigit((value >> 8) & 0xF);
        destination[4] = HexDigit((value >> 4) & 0xF);
        destination[5] = HexDigit(value & 0xF);
        charsWritten = 6;
        return true;
    }

    static bool MustEscape(int scalar) =>
        scalar <= 0x1F
        || scalar == '"'
        || scalar == '\\'
        || scalar is >= 0xD800 and <= 0xDFFF;

    static char HexDigit(int n) => (char)(n < 10 ? '0' + n : 'A' + (n - 10));
}
