# node-speak

Nonblocking text-to-speech announcements for AllStarLink ASL3. Submit text and
return to the command line; a systemd worker renders and plays announcements in
FIFO order across all nodes. Piper runs locally, and queue data and audio stay
in RAM.

One script contains the installer, submitter, and worker. No external queue
package, cloud TTS account, or Python packages are required.

## Requirements

- An ASL3 host running Debian Linux with systemd and Asterisk/app_rpt.
- Bash, Python 3.8 or newer, SoX, util-linux (`findmnt`), and standard utilities.
- The ASL3 package repository configured on the host.
- Root privileges for installation; root or the `asterisk` account for submission.

`--setup` checks the Debian package status of `asl3-tts`. If missing or only
partially installed, it runs `apt-get update` and `apt-get install -y asl3-tts`.
An already installed package is left alone, without refreshing package lists.
The package supplies Piper and the Amy low voice.
[Official ASL3 TTS instructions](https://allstarlink.github.io/adv-topics/tts/)

Setup verifies binaries and voice files before configuring mounts and the
service. If Python, SoX, or util-linux is missing, install them first:

```bash
sudo apt-get update
sudo apt-get install -y python3 sox util-linux
```


## Install

Download the script to your ASL3 host:

```bash
curl --fail --location --output node-speak \
  https://raw.githubusercontent.com/hackhamradio/node-speak/main/node-speak
sudo bash ./node-speak --setup
```

You can also download `node-speak` through GitHub's Raw view. Invoking the download
with Bash avoids needing `chmod +x`; setup installs an executable copy. If your
shell is already running as root, omit `sudo`.

Alternatively, clone the repository to obtain the documentation and tests too:

```bash
git clone https://github.com/hackhamradio/node-speak.git
cd node-speak
sudo bash ./node-speak --setup
```

Setup performs these operations:

1. Installs `asl3-tts` if needed.
2. Mounts a 32 MiB tmpfs at `/var/lib/asterisk/sounds/ramdisk`, with the Asterisk
   account's UID and primary GID and directory mode `0775`.
3. Adds or updates the exact tmpfs entry in `/etc/fstab`. Changed content is
   backed up to `/etc/fstab.node-speak.bak`.
4. Installs a root-owned executable at `/usr/local/bin/node-speak` and a systemd
   service at `/etc/systemd/system/node-speak.service`.
5. Reloads systemd and enables/restarts the worker, including startup on boot.

Setup rejects an existing mount or fstab entry for this path that is not tmpfs.
It stops on package installation or configuration errors.

Check that the service started:

```bash
sudo systemctl status node-speak.service
sudo journalctl -u node-speak.service -n 30 --no-pager
```

## Usage

As root or Asterisk, specify a local node number and quoted text:

```bash
/usr/local/bin/node-speak 12345 "Good morning. The repeater is online."
```

From an ordinary administrator account, run the submission as Asterisk:

```bash
sudo -u asterisk /usr/local/bin/node-speak 12345 "The net starts in five minutes."
```

Replace `12345` with a node configured on your host. The script validates the
number's format, but does not verify that the node exists or is ready. Playback
uses `rpt localplay`, for the local node rather than linked remote nodes.

Successful submission prints a UUID, for example:

```text
Queued job-67f83c3a-3ee6-4335-831c-17fc700e934d for node 12345
```

Exit status `0` means the request was queued. Rendering and playback happen
later; errors are logged in the service journal. Invalid input, a stopped worker,
a full queue, or a publication failure returns nonzero.

Submissions never wait for earlier announcements, rendering, playback, or cleanup.
Simultaneous submitters may briefly contend for an admission lock; that wait is
limited to two seconds. New jobs are checked every 0.2 seconds.

For shell variables, use double quotes:

```bash
message="The current date is $(date '+%B %d')."
/usr/local/bin/node-speak 12345 "$message"
```

Scripts invoked by Asterisk and entries in the Asterisk user's crontab can call
the installed script directly, without sudo. Use the absolute path to avoid
differences in their environment's `PATH`.

## Permissions

The worker runs as `asterisk` when that account exists, matching ASL3. On a host
without that account, it falls back to root.
[ASL3 permission model](https://allstarlink.github.io/adv-topics/permissions/)

Systemd creates `/run/node-speak` with mode `0770`, owned by the worker account
and its primary group. Queue files use mode `0660` and inherit that GID, including
when root submits them. Root, Asterisk, and other members of the primary group
can access the queue. No user systemd session, sudo rule, or PolicyKit change is
needed for submissions by root or Asterisk.

The worker's executable search path is
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
Piper and rendering dependencies must be accessible there. Custom voice files
and their parent directories must be readable/searchable by the worker.

## Configuration

Edit the configuration at the top of the downloaded script, then rerun setup.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VOICE` | `/var/lib/piper-tts/en_US-amy-low.onnx` | Piper model bundled with `asl3-tts` |
| `RAMDIR` | `/var/lib/asterisk/sounds/ramdisk` | Mounted tmpfs for generated audio |
| `RAMDISK_SIZE` | `32M` | Size used when mounting tmpfs; persisted in fstab |
| `PLAYBACK_GRACE` | `30` | Extra seconds audio remains available after its estimated duration |
| `QUEUE_LIMIT` | `64` | Maximum waiting jobs, excluding the active job |
| `MAX_TEXT_BYTES` | `16384` | Maximum UTF-8 bytes per submission |
| `JOB_TIMEOUT` | `600` | Maximum seconds per active job, including rendering and retention |

The voice requires a matching `.onnx.json` containing a positive integer
`audio.sample_rate`. The script reads that rate and converts the audio to 8 kHz
mono u-law for Asterisk. Node numbers must contain 1–32 digits; text must be
nonempty and contain no NUL characters.

Changing the tmpfs size in setup changes the persistent entry, but does not
resize an already mounted filesystem. Apply the new size at the next reboot or
through a separate remount when playback is idle.

Keep installation and runtime path constants at their defaults unless you also
adapt the systemd runtime directory definition.

## Queue lifecycle and ordering

One worker processes all nodes in global FIFO order. UUIDs identify jobs, while
a private monotonic-time prefix in queued filenames records admission order.
There is no persistent counter. Simultaneous requests are ordered by admission,
not by the instant their shell processes started.

Each job renders speech, submits playback, waits for the rounded-up audio
duration plus `PLAYBACK_GRACE`, and removes the audio and job record. Only then
does the next job start. The grace period adds time between announcements. An
idle queue normally contains only `worker.lock` and `submit.lock`; those files
remain intentionally and are not pending jobs.

Failed and timed-out jobs are discarded and logged, allowing later jobs to
continue. Waiting requests survive automatic restarts and `systemctl restart`.
An active job interrupted during restart is discarded rather than replayed,
because it may already have transmitted. Stopping the service explicitly or
rebooting clears the RAM queue. SIGKILL can leave temporary audio on tmpfs until
it is unmounted or the host reboots.

Asterisk's CLI does not confirm playback completion and can silently ignore
`localplay` for a missing or unready node. FIFO guarantees this worker's command
order; unrelated telemetry may delay actual playback beyond the retention
window. Increase `PLAYBACK_GRACE` if your node needs a longer window.

## Updates and service management

Download the latest script and rerun setup:

```bash
curl --fail --location --output node-speak \
  https://raw.githubusercontent.com/hackhamradio/node-speak/main/node-speak
sudo bash ./node-speak --setup
```

For a Git checkout, run `git pull --ff-only`, then setup from that checkout.
Reapply any configuration changes before installing an updated download. Setup
replaces the installed script and service and restarts the worker. Update when
playback is idle to avoid interrupting the active job. Running setup from the
old installed script does not download an update.

```bash
sudo systemctl restart node-speak.service
sudo systemctl stop node-speak.service
sudo systemctl start node-speak.service
sudo journalctl -u node-speak.service -f
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Queue not installed/running | Run setup, then inspect service status and the journal. |
| Cannot install `asl3-tts` | Check network connectivity and the configured ASL3 package repository. |
| Missing binary or unreadable model | Check the worker's `PATH`, `VOICE`, and Asterisk's read permissions. |
| tmpfs verification failure | Run setup as root; inspect `findmnt -M /var/lib/asterisk/sounds/ramdisk`. |
| Submission permission denied | Submit as root or Asterisk; inspect ownership of `/run/node-speak`. |
| Queue full | Allow waiting jobs to finish or adjust `QUEUE_LIMIT` and rerun setup. |
| Queued successfully, but no audio | Inspect the journal and verify the local node is configured and ready. Submission success does not confirm transmission. |

## Development

Run from the repository root, without sudo:

```bash
bash -n node-speak
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use real queue files and advisory locks while mocking package management,
Linux mounts, Piper, SoX, Asterisk, and systemd installation. They cover FIFO
submission, prompt return, queue limits, failures, timeouts, recovery, cleanup,
permissions, and setup. Live service startup, cross-account permissions, audio,
and radio playback need verification on the target ASL3 host.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributions and
[GitHub Issues](https://github.com/hackhamradio/node-speak/issues) for bug reports.

## License

[MIT](LICENSE). Piper, AllStarLink, and voice models retain their own licenses;
this repository does not redistribute those components.
