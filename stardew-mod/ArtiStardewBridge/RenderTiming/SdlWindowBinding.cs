using System.Runtime.InteropServices;

namespace ArtiStardewBridge.RenderTiming;

// SDL2 DesktopGL GameWindow.Handle is SDL_Window*, NOT HWND. Read-only Windows
// adapter, also linked into the disposable observer. No input/focus functions.
internal static class SdlWindowBinding
{
    // SDL 2.30.9 SDL_syswm.h: version[3], enum at 4, 64-byte union at 8.
    [StructLayout(LayoutKind.Explicit, Size = 72)]
    internal struct WmInfo
    {
        [FieldOffset(0)] public byte Major;
        [FieldOffset(1)] public byte Minor;
        [FieldOffset(2)] public byte Patch;
        [FieldOffset(4)] public int Subsystem;
        [FieldOffset(8)] public IntPtr Hwnd;
    }

    [DllImport("SDL2.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int SDL_GetWindowWMInfo(IntPtr window, ref WmInfo info);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);

    public static bool TryGetVisibleWindow(IntPtr sdlWindow, out IntPtr hwnd)
    {
        hwnd = IntPtr.Zero;
        if (!OperatingSystem.IsWindows() || IntPtr.Size != 8 || sdlWindow == IntPtr.Zero)
            return false;
        var info = new WmInfo { Major = 2, Minor = 0, Patch = 0 };
        if (SDL_GetWindowWMInfo(sdlWindow, ref info) != 1 || info.Subsystem != 1
            || info.Hwnd == IntPtr.Zero || !IsWindow(info.Hwnd))
            return false;
        if (GetWindowThreadProcessId(info.Hwnd, out uint pid) == 0 || pid != Environment.ProcessId
            || !IsWindowVisible(info.Hwnd) || IsIconic(info.Hwnd))
            return false;
        hwnd = info.Hwnd;
        return true;
    }
}
