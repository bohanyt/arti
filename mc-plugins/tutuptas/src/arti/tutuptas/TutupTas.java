package arti.tutuptas;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Satu perintah: menutup layar/GUI yang sedang terbuka di klien seorang pemain.
 *
 * Kenapa ini ada. Menampilkan isi tas Arti di siaran dibuat dengan `sudo player
 * <kamera> invsee arti_berarti` — itu MEMBUKA jendela di klien kamera. Masalahnya
 * menutupnya lagi: diuji satu per satu di server uji, TIDAK ADA
 * perintah vanilla yang bisa menutup jendela pemain lain. Yang berhasil cuma
 * pindah dimensi dan mati, dan dua-duanya merusak siaran. Yang gagal: tp,
 * ganti gamemode, spectate, blindness, naik entitas, damage, clear.
 *
 * Bukkit sendiri cuma butuh satu panggilan: Player#closeInventory(). Jadi
 * daripada menambah plugin pihak ketiga lagi, ini ditulis sendiri supaya
 * seluruh isinya bisa dibaca dalam satu layar.
 *
 * Pemakaian (dari konsol/RCON):  tutuptas <pemain>
 */
public final class TutupTas extends JavaPlugin {

    @Override
    public boolean onCommand(CommandSender pengirim, Command perintah,
                             String label, String[] args) {
        if (args.length != 1) {
            pengirim.sendMessage("Pemakaian: /tutuptas <pemain>");
            return true;
        }
        Player pemain = Bukkit.getPlayerExact(args[0]);
        if (pemain == null) {
            // Bukan error fatal: kamera yang belum join itu keadaan normal.
            pengirim.sendMessage("tutuptas: '" + args[0] + "' tidak online");
            return true;
        }
        // HARUS di thread utama. Pemanggilnya RCON, yang jalan di thread
        // sendiri; menyentuh entity dari sana bisa merusak state server.
        Bukkit.getScheduler().runTask(this, (Runnable) () -> pemain.closeInventory());
        pengirim.sendMessage("tutuptas: layar " + pemain.getName() + " ditutup");
        return true;
    }
}
