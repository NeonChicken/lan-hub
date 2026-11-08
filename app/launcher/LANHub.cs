// LAN Hub launcher: starts the bundled Python server (app\tools\lanhub.py) without a console
// window, opens the hub in your browser and sits in the system tray (Open / Folders / Rescan / Quit).
//
// What it does, and nothing more:
//   * runs app\runtime\python\python.exe (signed by the Python Software Foundation) with app\tools\lanhub.py
//   * the server process is tied to this launcher with a Windows Job Object: quit = server gone
//   * writes app\data\launcher.log (the server's console output) for troubleshooting
//   * talks only to http://127.0.0.1:<port> (the local hub)
//   * updates: when the server has downloaded and verified an update (app\data\update\ready.json) it exits
//     with code 75; the launcher then restarts and, before starting the server, runs
//     "python app\tools\updater.py --apply" which moves the verified files into place
//
// Build (no Visual Studio needed): app\launcher\build.bat  (uses csc.exe that ships with Windows)
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("LAN Hub")]
[assembly: AssemblyProduct("LAN Hub")]
[assembly: AssemblyDescription("LAN Hub game launcher: starts the local hub and shows a tray icon")]
[assembly: AssemblyCopyright("(c) 2026 NeonChicken, MIT License. Source: github.com/NeonChicken/lan-hub")]
[assembly: AssemblyCompany("NeonChicken")]
[assembly: AssemblyVersion("2.1.1.0")]
[assembly: AssemblyFileVersion("2.1.1.0")]
[assembly: AssemblyInformationalVersion("2.1.1")]

namespace LANHub
{
    static class Program
    {
        static string HubDir, AppDir, LogPath, Url;
        static int Port = 7777;
        static Process Server;
        static NotifyIcon Tray;
        static StreamWriter Log;
        static readonly object LogLock = new object();
        static string LanIp = null;
        static bool Quitting = false, Ready = false, Restart = false, Quiet = false;
        const int RESTART_CODE = 75;   // the server asks for a restart (an update is staged)
        static readonly System.Collections.Generic.List<string> Tail = new System.Collections.Generic.List<string>();
        static SynchronizationContext Ui;

        [STAThread]
        static void Main(string[] args)
        {
            Quiet = Array.IndexOf(args, "--quiet") >= 0;   // restarted after an update: the open page reconnects by itself
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            HubDir = Path.GetDirectoryName(Application.ExecutablePath);
            AppDir = Path.Combine(HubDir, "app");
            Port = ReadPort();
            try   // a running hub may have moved to another port (7777 busy): it writes the real one here
            {
                string pf = Path.Combine(AppDir, "data\\port.txt");
                int rp;
                if (File.Exists(pf) && int.TryParse(File.ReadAllText(pf).Trim(), out rp) && rp > 1023 && rp < 65536) Port = rp;
            }
            catch { }
            Url = "http://127.0.0.1:" + Port + "/";   // never "localhost": that may resolve to ::1

            bool created;
            using (var mutex = new Mutex(true, "Local\\LANHub-" + (HubDir.ToLowerInvariant().GetHashCode() & 0x7fffffff), out created))
            {
                if (!created) { if (IsLanHub()) OpenBrowser(""); return; }   // already running: just show it
                string py = Path.Combine(AppDir, "runtime\\python\\python.exe");
                string script = Path.Combine(AppDir, "tools\\lanhub.py");
                if (!File.Exists(script))
                {
                    Fail("The 'app' folder is missing next to LAN Hub.exe.\n\nExtract the whole zip, not just the .exe.");
                    return;
                }
                if (!File.Exists(py))
                {
                    Fail("The bundled Python (app\\runtime\\python) is missing.\n\nRe-extract the zip, or run 'app\\Start LAN Hub (fallback).bat'.");
                    return;
                }
                if (ApplyUpdate(py)) Restart = true;   // LAN Hub.exe itself was replaced: start the new one
                else
                {
                    Ui = new WindowsFormsSynchronizationContext();
                    SynchronizationContext.SetSynchronizationContext(Ui);
                    SetupTray();
                    StartServer(py, script);
                    Application.Run();
                    Shutdown();
                }
            }
            if (Restart)   // the mutex is released here, so the new instance can take over
            {
                try { Process.Start(new ProcessStartInfo(Application.ExecutablePath, "--quiet") { WorkingDirectory = HubDir }); }
                catch (Exception ex) { Fail("Please start LAN Hub again.\n\n" + ex.Message); }
            }
        }

        // Moves a downloaded + verified update into place (app\tools\updater.py --apply). True = restart needed.
        static bool ApplyUpdate(string py)
        {
            string upd = Path.Combine(AppDir, "data\\update");
            if (!File.Exists(Path.Combine(upd, "ready.json")) && !File.Exists(Path.Combine(upd, "old.json"))) return false;
            try
            {
                var psi = new ProcessStartInfo(py, "-I \"" + Path.Combine(AppDir, "tools\\updater.py") + "\" --apply");
                psi.WorkingDirectory = AppDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.StandardOutputEncoding = Encoding.UTF8;
                psi.StandardErrorEncoding = Encoding.UTF8;
                using (var p = Process.Start(psi))
                {
                    var err = p.StandardError.ReadToEndAsync();
                    string outp = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(300000);
                    try { File.WriteAllText(Path.Combine(AppDir, "data\\update-apply.log"), outp + err.Result, new UTF8Encoding(false)); } catch { }
                    if (outp.Contains("UPDATE_FAILED"))
                        Fail("The update couldn't be installed, so LAN Hub keeps the version you had.\n\n" + outp.Trim());
                    return outp.Contains("RESTART_LAUNCHER");
                }
            }
            catch (Exception ex) { Fail("The update couldn't be installed:\n" + ex.Message); return false; }
        }

        static int ReadPort()
        {
            try
            {
                string cfg = File.ReadAllText(Path.Combine(AppDir, "config.json"));
                Match m = Regex.Match(cfg, "\"port\"\\s*:\\s*(\\d{2,5})");
                if (m.Success) { int p = int.Parse(m.Groups[1].Value); if (p > 1023 && p < 65536) return p; }
            }
            catch { }
            return 7777;
        }

        // ------------------------------------------------------------ server process
        static void StartServer(string py, string script)
        {
            Directory.CreateDirectory(Path.Combine(AppDir, "data"));
            LogPath = Path.Combine(AppDir, "data\\launcher.log");
            Log = new StreamWriter(LogPath, false, new UTF8Encoding(false));
            Log.AutoFlush = true;

            var psi = new ProcessStartInfo(py, "-I \"" + script + "\" --no-browser");
            psi.WorkingDirectory = AppDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;
            psi.EnvironmentVariables["LANHUB_LAUNCHER"] = "1";   // the server may ask us to restart (exit code 75)

            Server = new Process();
            Server.StartInfo = psi;
            Server.EnableRaisingEvents = true;
            Server.OutputDataReceived += delegate (object s, DataReceivedEventArgs e) { OnLine(e.Data); };
            Server.ErrorDataReceived += delegate (object s, DataReceivedEventArgs e) { OnLine(e.Data); };
            Server.Exited += delegate { Ui.Post(delegate { OnServerExit(); }, null); };
            try { Server.Start(); }
            catch (Exception ex) { Fail("Could not start the LAN Hub server:\n" + ex.Message); Application.Exit(); return; }
            Job.Attach(Server);   // if the launcher dies, Windows kills the server too
            Server.BeginOutputReadLine();
            Server.BeginErrorReadLine();
        }

        static void OnLine(string line)
        {
            if (line == null) return;
            lock (LogLock)
            {
                try { Log.WriteLine(DateTime.Now.ToString("HH:mm:ss ") + line); } catch { }
                Tail.Add(line); if (Tail.Count > 25) Tail.RemoveAt(0);
            }
            if (line.StartsWith("READY "))
            {
                string u = line.Substring(6).Trim();
                if (Regex.IsMatch(u, "^http://127\\.0\\.0\\.1:\\d{4,5}/$")) Url = u;  // the server's real port wins
                Ready = true;
                Ui.Post(delegate
                {
                    Tray.Text = "LAN Hub: running";
                    if (Quiet) { Tray.ShowBalloonTip(3000, "LAN Hub updated", "Your open LAN Hub page reconnects by itself.", ToolTipIcon.Info); return; }
                    OpenBrowser("");
                    Tray.ShowBalloonTip(4000, "LAN Hub is running", "It lives here in the tray. Right-click for options, or Quit when you're done.", ToolTipIcon.Info);
                }, null);
            }
            else if (line.StartsWith("ALREADY_RUNNING"))
            {
                bool ours = IsLanHub();
                Ui.Post(delegate
                {
                    if (ours) OpenBrowser("");
                    else Fail("Port " + Port + " is already used by another program, so LAN Hub can't start.\n\nClose that program, or change \"port\" in app\\config.json.");
                    Quitting = true; Application.Exit();
                }, null);
            }
            else
            {
                Match m = Regex.Match(line, "Your LAN IP: ([0-9.]+)");
                if (m.Success) Ui.Post(delegate { LanIp = m.Groups[1].Value; RefreshMenu(); }, null);
            }
        }

        static void OnServerExit()
        {
            if (Quitting) return;
            int code = -1;
            try { code = Server.ExitCode; } catch { }
            if (code == RESTART_CODE) { Restart = true; Quitting = true; Application.Exit(); return; }   // update staged
            string tail;
            lock (LogLock) { tail = string.Join("\n", Tail.ToArray()); }
            if (tail.Contains("ALREADY_RUNNING")) { Application.Exit(); return; }
            Fail("The LAN Hub server stopped unexpectedly.\n\nLast output:\n" + tail + "\n\nFull log: " + LogPath);
            Application.Exit();
        }

        static void Shutdown()
        {
            Quitting = true;
            try { if (Server != null && !Server.HasExited) Server.Kill(); } catch { }
            if (Tray != null) { Tray.Visible = false; Tray.Dispose(); }
            try { if (Log != null) Log.Dispose(); } catch { }
        }

        // ------------------------------------------------------------ tray
        static ToolStripMenuItem ipItem;

        static void SetupTray()
        {
            Tray = new NotifyIcon();
            try { Tray.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { Tray.Icon = SystemIcons.Application; }
            Tray.Text = "LAN Hub: starting…";
            var menu = new ContextMenuStrip();
            var open = new ToolStripMenuItem("Open LAN Hub", null, delegate { OpenBrowser(""); });
            open.Font = new Font(open.Font, FontStyle.Bold);
            menu.Items.Add(open);
            menu.Items.Add(new ToolStripMenuItem("Game folders && permissions…", null, delegate { OpenBrowser("#settings"); }));
            menu.Items.Add(new ToolStripMenuItem("Rescan games", null, delegate { Rescan(); }));
            ipItem = new ToolStripMenuItem("Your LAN IP: …", null, delegate { if (LanIp != null) { Clipboard.SetText(LanIp); Tray.ShowBalloonTip(2000, "Copied", LanIp + " is on your clipboard", ToolTipIcon.None); } });
            menu.Items.Add(ipItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Show log file", null, delegate { if (LogPath != null) Process.Start(Path.Combine(Environment.SystemDirectory, "notepad.exe"), "\"" + LogPath + "\""); }));
            menu.Items.Add(new ToolStripMenuItem("Quit LAN Hub", null, delegate { Quitting = true; Application.Exit(); }));
            Tray.ContextMenuStrip = menu;
            Tray.DoubleClick += delegate { OpenBrowser(""); };
            Tray.Visible = true;
        }

        static void RefreshMenu()
        {
            if (ipItem != null) ipItem.Text = LanIp != null ? "Your LAN IP: " + LanIp + "  (click to copy)" : "Your LAN IP: …";
        }

        static void OpenBrowser(string hash)
        {
            if (Environment.GetEnvironmentVariable("LANHUB_NO_BROWSER") == "1") return;  // for automated tests
            try { Process.Start(Url + hash); } catch (Exception ex) { MessageBox.Show("Open " + Url + " in your browser.\n\n" + ex.Message, "LAN Hub"); }
        }

        // Is the program on our port really LAN Hub (and not something else squatting it)?
        static bool IsLanHub()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(Url + "api/ping");
                req.Proxy = null; req.Timeout = 3000;
                using (var resp = (HttpWebResponse)req.GetResponse())
                    return (resp.Headers["Server"] ?? "").StartsWith("LANHub");
            }
            catch { return false; }
        }

        static void Rescan()
        {
            if (!Ready) return;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(Url + "api/rescan");
                    req.Method = "POST";
                    req.ContentType = "application/json";
                    req.Headers.Add("X-LANHub", "1");
                    req.Proxy = null;
                    byte[] body = Encoding.UTF8.GetBytes("{}");
                    req.ContentLength = body.Length;
                    using (var s = req.GetRequestStream()) s.Write(body, 0, body.Length);
                    using (req.GetResponse()) { }
                    Ui.Post(delegate { Tray.ShowBalloonTip(2500, "Rescanning", "The hub updates by itself when it's done.", ToolTipIcon.None); }, null);
                }
                catch (Exception ex) { Ui.Post(delegate { Tray.ShowBalloonTip(3000, "Rescan failed", ex.Message, ToolTipIcon.Warning); }, null); }
            });
        }

        static void Fail(string msg)
        {
            MessageBox.Show(msg, "LAN Hub", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    // Kill-on-close job object: the server can never outlive the launcher.
    static class Job
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string name);
        [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint len);
        [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);

        [StructLayout(LayoutKind.Sequential)]
        struct BASIC { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinWs, MaxWs; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)]
        struct IO { public ulong a, b, c, d, e, f; }
        [StructLayout(LayoutKind.Sequential)]
        struct EXT { public BASIC Basic; public IO Io; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }

        static IntPtr handle = IntPtr.Zero;

        public static void Attach(Process p)
        {
            try
            {
                if (handle == IntPtr.Zero)
                {
                    handle = CreateJobObject(IntPtr.Zero, null);
                    var info = new EXT();
                    // KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK: only the server is in the job. Games it launches are
                    // NOT, so quitting LAN Hub never closes a running game.
                    info.Basic.LimitFlags = 0x2000 | 0x1000;
                    int len = Marshal.SizeOf(typeof(EXT));
                    IntPtr ptr = Marshal.AllocHGlobal(len);
                    Marshal.StructureToPtr(info, ptr, false);
                    SetInformationJobObject(handle, 9, ptr, (uint)len);
                    Marshal.FreeHGlobal(ptr);
                }
                AssignProcessToJobObject(handle, p.Handle);
            }
            catch { }
        }
    }
}
