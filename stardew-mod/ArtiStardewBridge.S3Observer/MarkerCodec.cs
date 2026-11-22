namespace ArtiS3Observer;

// 8x8 black/white cells, MSB first. D3 | nonce16 | draw24 | CRC16-CCITT.
internal static class MarkerCodec
{
    public static ulong Encode(ushort nonce, uint draw)
    {
        if (nonce == 0 || draw == 0 || draw > 0xFFFFFF) throw new ArgumentOutOfRangeException();
        ulong payload = (0xD3UL << 40) | ((ulong)nonce << 24) | draw;
        ushort crc = 0xFFFF;
        for (int b = 5; b >= 0; b--)
        {
            crc ^= (ushort)(((payload >> (b * 8)) & 0xFF) << 8);
            for (int bit = 0; bit < 8; bit++)
                crc = (ushort)((crc & 0x8000) != 0 ? (crc << 1) ^ 0x1021 : crc << 1);
        }
        return (payload << 16) | crc;
    }
}
