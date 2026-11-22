using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

public sealed class BridgeClient : IAsyncDisposable
{
    private readonly string host;
    private readonly int port;
    private readonly SemaphoreSlim gate = new(1, 1);
    private TcpClient? client;
    private StreamWriter? writer;
    private long sequence;
    private bool helloSent;
    private bool disposed;

    public BridgeClient(string host, int port)
    {
        if (!string.Equals(host, "127.0.0.1", StringComparison.Ordinal))
            throw new ArgumentException("Stardew bridge hanya boleh memakai IPv4 loopback.", nameof(host));
        if (port is < 1 or > 65535)
            throw new ArgumentOutOfRangeException(nameof(port));

        this.host = host;
        this.port = port;
    }

    /// <summary>
    /// Sudahkah hello sukses di koneksi yang SEDANG hidup?
    ///
    /// Dibaca ModEntry di game thread untuk memilih hello vs state. Sengaja
    /// tanpa gate: ini bool, tidak bisa terbaca robek. Kalau nilainya sudah
    /// basi saat dipakai, jalur yang dipanggil tetap memeriksanya lagi di
    /// dalam gate dan mengembalikan false -- jadi tidak ada state yang bisa
    /// menyelinap sebelum hello.
    /// </summary>
    public bool HelloSent => this.helloSent;

    public async Task<bool> EnsureHelloAsync(
        Func<long, HelloEnvelope?> createHello,
        CancellationToken cancellationToken = default
    )
    {
        await this.gate.WaitAsync(cancellationToken);
        try
        {
            if (this.disposed)
                return false;
            if (this.ConnectionClosed())
                this.CloseConnection();
            if (this.helloSent)
                return false;

            long nextSequence = this.sequence + 1;
            HelloEnvelope? hello = createHello(nextSequence);
            if (hello is null)
                return false;
            if (this.client is null)
                await this.ConnectAsync(cancellationToken);

            string line = JsonSerializer.Serialize(hello, ProtocolJson.Options);
            await this.writer!.WriteLineAsync(line);
            await this.writer.FlushAsync();
            this.sequence = nextSequence;
            this.helloSent = true;
            return true;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            this.CloseConnection();
            return false;
        }
        catch (IOException)
        {
            this.CloseConnection();
            return false;
        }
        catch (SocketException)
        {
            this.CloseConnection();
            return false;
        }
        finally
        {
            this.gate.Release();
        }
    }

    /// <summary>
    /// Kirim satu envelope SESUDAH hello, di koneksi yang SAMA.
    ///
    /// Invarian yang dijaga di sini (Gate 5):
    ///   1. hello harus lebih dulu -- tanpa itu, kembalikan false;
    ///   2. reconnect membatalkan hello, jadi data ikut ditolak sampai
    ///      hello dikirim ulang;
    ///   3. sequence naik HANYA sesudah tulis berhasil;
    ///   4. satu penulis saja (gate yang sama dengan hello);
    ///   5. factory boleh mengembalikan null -- tidak memakan sequence.
    ///
    /// SENGAJA tidak membuka koneksi sendiri: kalau socket belum ada, itu
    /// berarti hello belum pernah sukses, dan data tidak boleh mendahuluinya.
    ///
    /// Overload StateEnvelope tetap ada supaya call site motor tidak berubah.
    /// Observation memakai writer yang sama, bukan socket kedua.
    /// </summary>
    public Task<bool> SendAfterHelloAsync(
        Func<long, StateEnvelope?> createState,
        CancellationToken cancellationToken = default
    ) => this.SendAfterHelloAsync<StateEnvelope>(createState, cancellationToken);

    public Task<bool> SendAfterHelloAsync(
        Func<long, ObservationEnvelope?> createObservation,
        CancellationToken cancellationToken = default
    ) => this.SendAfterHelloAsync<ObservationEnvelope>(createObservation, cancellationToken);

    public async Task<bool> SendAfterHelloAsync<TEnvelope>(
        Func<long, TEnvelope?> createEnvelope,
        CancellationToken cancellationToken = default
    ) where TEnvelope : class
    {
        await this.gate.WaitAsync(cancellationToken);
        try
        {
            if (this.disposed)
                return false;
            if (this.ConnectionClosed())
            {
                this.CloseConnection();
                return false;
            }
            if (!this.helloSent || this.writer is null)
                return false;

            long nextSequence = this.sequence + 1;
            TEnvelope? envelope = createEnvelope(nextSequence);
            if (envelope is null)
                return false;

            string line = JsonSerializer.Serialize(envelope, ProtocolJson.Options);
            await this.writer.WriteLineAsync(line);
            await this.writer.FlushAsync();
            this.sequence = nextSequence;
            return true;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            this.CloseConnection();
            return false;
        }
        catch (IOException)
        {
            this.CloseConnection();
            return false;
        }
        catch (SocketException)
        {
            this.CloseConnection();
            return false;
        }
        finally
        {
            this.gate.Release();
        }
    }

    private async Task ConnectAsync(CancellationToken cancellationToken)
    {
        TcpClient nextClient = new();
        using CancellationTokenSource timeout = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken
        );
        timeout.CancelAfter(TimeSpan.FromSeconds(1));
        try
        {
            await nextClient.ConnectAsync(this.host, this.port, timeout.Token);
        }
        catch
        {
            nextClient.Dispose();
            throw;
        }

        this.client = nextClient;
        this.writer = new StreamWriter(
            nextClient.GetStream(),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
            bufferSize: 1024,
            leaveOpen: true
        );
        this.helloSent = false;
    }

    private bool ConnectionClosed()
    {
        if (this.client is null)
            return false;
        if (!this.client.Connected)
            return true;

        Socket socket = this.client.Client;
        return socket.Poll(0, SelectMode.SelectRead) && socket.Available == 0;
    }

    private void CloseConnection()
    {
        this.helloSent = false;
        this.writer?.Dispose();
        this.client?.Dispose();
        this.writer = null;
        this.client = null;
    }

    public async ValueTask DisposeAsync()
    {
        await this.gate.WaitAsync();
        try
        {
            if (this.disposed)
                return;
            this.disposed = true;
            this.CloseConnection();
        }
        finally
        {
            this.gate.Release();
        }
    }
}
