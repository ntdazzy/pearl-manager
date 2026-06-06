# Pearl Miner Manager

Production-oriented manager for Pearl mining on AlphaPool with FastAPI, PostgreSQL, Telegram inline controls, live dashboard, watchdog, and RTX 3060 overclock profiles.

## AlphaPool Defaults

- Stratum host: `sg1.alphapool.tech`
- Stratum port: `5566`
- Miner API: `https://pearl.alphapool.tech/api/miner/{wallet}`
- Miner API fallback: `POOL_API_FALLBACK_URLS` can contain comma/space-separated fallback templates such as Miningcore-style `/api/pools/pearl/miners/{wallet}`.
- Pool stats: `https://pearl.alphapool.tech/api/stats`
- Price API: `https://api.prlscan.com/v1/market/prl`
- Miner runtime: `alpha-miner` by default. SRBMiner-compatible systemd generation remains available with `MINER_TYPE=srbminer`, but AlphaPool's current Pearl endpoint is verified with `alpha-miner`.

## Deploy

1. Edit `config.env`: set `WALLET_ADDRESS`, check `POOL_HOST`/`POOL_PORT`, and set `TELEGRAM_TOKEN` plus `TELEGRAM_CHAT_ID` if Telegram control is needed.
2. Keep `WEB_HOST=127.0.0.1` for local-only access. If you expose the dashboard on LAN with `WEB_HOST=0.0.0.0`, set a strong `CONTROL_API_TOKEN`; remote dashboard page, data, live logs, and control endpoints reject requests without it. Open once with `http://<rig-ip>:8555/?token=<CONTROL_API_TOKEN>` on your phone so the browser can remember the token.
3. Run:

```bash
./setup_env.sh
./start_all.sh
```

`deploy.sh` is a one-command wrapper around `setup_env.sh` for the A-Z deployment flow:

```bash
./deploy.sh
```

Dashboard:

```text
http://localhost:8555
```

Remote dashboard access is intentionally blocked when `WEB_HOST=0.0.0.0` and `CONTROL_API_TOKEN` is empty.

## Safe Setup Validation

Generate and validate service/sudoers files without changing the machine:

```bash
DRY_RUN=1 DRY_RUN_DIR=/tmp/pearl-dry-run ./setup_env.sh
```

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -v
./venv/bin/python -m py_compile config.py models.py database.py miner_services.py app.py telegram_bot.py telegram_controller.py
bash -n setup_env.sh deploy.sh install.sh start_all.sh stop_all.sh pearl-manager.sh verify_deploy.sh
```

After real installation, run:

```bash
./verify_deploy.sh
```

## Notes

- `config.env` is ignored by git because it may contain Telegram credentials.
- Quote values that contain spaces in `config.env`, for example `MINER_EXTRA_ARGS="--api-enable --api-port 21550"`.
- Watchdog stops the miner if temperature exceeds `TEMP_SHUTDOWN_C` for repeated checks, if local miner hashrate is zero for repeated checks and `HASHRATE_ZERO_STOP=1`, or if the miner service is active but the miner process is missing. AlphaPool API outage/stale hashrate only triggers alerts by default.
- OC profiles support pseudo-undervolting through `nvidia-smi`: persistence mode, power limit, optional `--lock-gpu-clocks`, then `nvidia-settings` core/memory offsets. The default Balance profile uses `115W` and `1450,1450 MHz`.
- `STARTUP_OC_PROFILE=balance` makes `pearl-miner.service` apply the power limit and GPU clock lock before mining starts after boot/restart.
- Dashboard shows local hashrate as `0 H/s` when `pearl-miner.service` is stopped, even if AlphaPool still caches a recent worker hashrate.
