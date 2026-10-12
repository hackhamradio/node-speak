# Contributing

Open an issue for a bug or proposed behavior change. Include your ASL3 and
Debian versions, the command used, and relevant journal output. Remove private
information before posting logs.

Keep installation, submission, and worker logic in the `node-speak` executable.
Preserve nonblocking submission, global FIFO order, RAM storage, and access for
root and Asterisk without adding sudo or PolicyKit rules.

Before opening a pull request, run:

```bash
bash -n node-speak
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Add tests for meaningful behavior changes, using isolated temporary directories
and mocked external services. Tests must not install packages, mount filesystems,
modify host services, or transmit audio. Describe any live ASL3 validation
separately in the pull request.

Update the README when commands, dependencies, configuration, permissions, or
queue behavior change. Use concise commit messages describing the change.

Contributions are made under the repository's MIT license.
