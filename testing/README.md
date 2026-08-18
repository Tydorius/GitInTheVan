# Cross-platform test harness

Provisions a throwaway GitInTheVan install on a target machine, exercises it,
archives the logs, and deletes itself. One command per platform instead of a
manual afternoon.

It deliberately runs the repo's **real** deploy scripts rather than
reimplementing them: the end-user install path is what is being verified, so
anything that diverges here would test the wrong thing.

## Setup

```
cp testing/harness.env.example testing/harness.env
```

Fill in the hosts. `harness.env` is gitignored — it holds machine addresses and
a throwaway admin password.

SSH must already work without a password prompt (`ssh <target> whoami` should
print a username and exit). The harness runs with `BatchMode=yes`, so a missing
key fails fast instead of hanging on a prompt.

The Windows target is special: set `TARGET_WINDOWS=localhost` and no SSH is used
at all — commands run directly on this machine.

## Usage

```
remote-test.bat -env .\testing\harness.env -target linux -branch main up test hold
   ... test by hand from any machine on the network ...
remote-test.bat -env .\testing\harness.env -target linux logs down
```

Or unattended:

```
remote-test.bat -env .\testing\harness.env -target docker -branch main all
```

On macOS/Linux, call `python testing/harness.py` directly with the same
arguments.

| Subcommand | Does |
|---|---|
| `up` | clone at `-branch`, run the real deploy script, start the mock upstream, create the admin user, seed an endpoint, wait for health |
| `test` | run `scripts/flow_test.py` against the instance, then cross-network checks from this machine |
| `hold` | print the URL and credentials, then wait for Enter |
| `logs` | pull logs into `testing/artifacts/<run-id>/`, redact credentials, scan for problems |
| `down` | stop the instance and delete the run directory |
| `all` | `up test logs down`, no hold |

Subcommands chain and run left to right. Run state is written to
`testing/runs/<run-id>.json`, so `logs` and `down` work from a later session —
they pick the most recent run for the target unless you pass `-run <run-id>`.

| Flag | Meaning |
|---|---|
| `-env` | config file (default `testing/harness.env`) |
| `-target` | `macos`, `linux`, `docker`, or `windows` |
| `-branch` | branch to clone; defaults to `BRANCH` in the config |
| `-run` | operate on a specific run id rather than the latest |
| `-replicate` | copy endpoints from the local database instead of using the mock upstream |
| `-jump` | ProxyJump host for this run, overriding the config; `-jump none` connects directly |

### Jump hosts

Set a jump per target rather than globally — a single `SSH_JUMP` would also
route directly reachable machines through the bastion:

```
MACOS_SSH_JUMP=                    # directly reachable, no jump
LINUX_SSH_JUMP=root@10.0.0.1
DOCKER_SSH_JUMP=root@10.0.0.1
```

Precedence is `-jump`, then `<TARGET>_SSH_JUMP`, then `SSH_JUMP`. A
target-specific key that is present but empty means "no jump" — that is how you
opt one target out of a global fallback.

Note that a **port** cannot go in `SSH_OPTS`: that string is shared between
`ssh` and `scp`, which disagree (`ssh -p` is the port, `scp -p` preserves
timestamps). Use a `~/.ssh/config` host alias, which both honour, and which can
carry `ProxyJump` too:

```
Host gitv-linux
    HostName dock-21
    Port 2222
    User linuxuser
    ProxyJump root@10.0.0.1
    IdentityFile ~/.ssh/id_ed25519_gitvlinux
```

Then `TARGET_LINUX=gitv-linux` and no jump setting is needed at all.

Exit codes: `0` pass, `1` a test or log scan failed, `2` harness error
(bad config, unreachable target, provisioning failure).

## Targets

| Target | Exercises |
|---|---|
| `macos` | `scripts/deploy-macos.sh` |
| `linux` | `scripts/deploy-linux.sh` |
| `docker` | `Dockerfile` + `docker-compose.*.yml`, run **directly on** a Docker host — nothing is nested |
| `windows` | `scripts\deploy-windows.bat`, on this machine |

## The mock upstream

By default the harness starts `testing/remote/mock_upstream.py` on the target — a
stdlib-only OpenAI-compatible stub — and points the seeded endpoint at it.

That is the default for a reason. `Endpoint.api_key` is stored in plaintext
(`app/models/endpoint.py`), so replicating real endpoints copies live billable
credentials onto throwaway machines. The mock also makes runs deterministic,
free, and possible with no internet.

`-replicate` copies your local endpoints when you genuinely need a real provider.
The log archiver redacts credential-shaped strings on the way out, but the keys
still sit in the test instance's database until teardown.

## Safety

Teardown deletes directories, so it is gated four ways. A run directory is
always `<FOLDER>/_gitv-testruns/<run-id>/`, and `down` refuses unless:

1. the path contains `_gitv-testruns`,
2. the path contains its own run id,
3. it is not the configured parent folder, a drive root, `/`, `~`, or `.`,
4. a `.gitv-testrun` marker file exists **on the target** whose contents match
   the run id — checked immediately before removal, so a stale local state file
   cannot aim a delete at the wrong directory.

`testing/artifacts/` is never touched by teardown. Delete archives yourself when
you no longer need them.

## Notes

- The deploy scripts end by running the server in the foreground, so the harness
  backgrounds them and decides readiness by polling `/health` until it answers
  `{"status":"ok"}` twice. One response proves nothing — the updater's
  maintenance page binds the same port and answers every path. The deploy exit
  code is not trusted on its own either: it exits `0` when the port is already
  in use.
- `ADMIN_PASSWORD` must satisfy the app's own rules: 8+ characters with at least
  one letter and one digit. The harness checks this before provisioning rather
  than letting `/api/auth/setup` fail with a bare 400.
- `GITV_PORT` defaults to `8100`, off the standard `8000`, so a run cannot
  collide with a real instance on the target.
- For Docker, `flow_test.py` runs from *this* machine rather than on the target:
  `scripts/` is not copied into the image, so there is no in-container copy.
