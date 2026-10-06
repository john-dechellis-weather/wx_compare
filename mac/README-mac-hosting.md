# Hosting BlueMet on the Mac mini (M4, 16 GB)

Order matters. Each step ends with a check. Do not move the domain
(step 7) until step 6 works on a test hostname.

Replace YOURNAME with your macOS short user name everywhere
(`whoami` prints it).

## 1. Mac settings, so it stays up

System Settings -> Energy:
- Prevent automatic sleeping when the display is off: ON
- Start up automatically after a power failure: ON
- Wake for network access: ON

System Settings -> General -> Software Update -> (i): turn OFF
"Install macOS updates" automatic installs (updates reboot the Mac at
their own time). Leave security responses on.

System Settings -> Users & Groups -> Automatically log in as: your
account (so a reboot comes back without a keyboard).

Terminal, belt and braces:

    sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1 womp 1

Check: `pmset -g` shows sleep 0 and autorestart 1.

## 2. Tools

    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    brew install git cloudflared
    curl -L -o ~/Miniforge3.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-arm64.sh
    bash ~/Miniforge3.sh -b -p ~/miniforge3
    ~/miniforge3/bin/conda init zsh
    # close and reopen Terminal

Check: `conda --version` and `cloudflared --version` both print.

Miniforge (conda-forge) is used because eccodes, cfgrib, cartopy,
netCDF4 and Py-ART all ship as ready-made Apple Silicon builds there;
pip would try to compile some of them.

## 3. The repo and its Python environment

    mkdir -p ~/bluemet && cd ~/bluemet
    git clone https://github.com/john-dechellis-weather/wx_compare.git
    cd wx_compare
    conda create -y -n bluemet python=3.12
    conda activate bluemet
    conda install -y -c conda-forge eccodes cfgrib cartopy pyproj netcdf4 h5py xarray scipy pandas numpy matplotlib metpy arm_pyart
    pip install -r requirements.txt

If `arm_pyart` fails, install everything else and run
`pip install arm_pyart` afterwards; it only feeds the L2 Radar Lab.

Check:

    python -c "import cfgrib, cartopy, metpy, streamlit; print('ok')"

## 4. Cache directory and keys

The site keeps its warm store under the path Render mounts its disk
at. Creating the same path on the Mac means zero code changes:

    sudo mkdir -p /opt/render/project/src/cache
    sudo chown -R $(whoami) /opt/render/project

Keys and the password go in `mac/.env` (copy `mac/.env.example`, fill
every variable that exists on Render -> Environment). It is listed in
.gitignore-worthy territory: never commit it.

## 5. First run, by hand

    cd ~/bluemet/wx_compare
    chmod +x mac/run_bluemet.sh
    mac/run_bluemet.sh

Open http://localhost:8501 on the Mac. Log in, open the JBU Weather
Map, watch `Background warmers` under More for a few minutes. Ctrl-C
to stop.

## 6. Cloudflare Tunnel (no port forwarding, HTTPS included)

6a. Cloudflare account (free): https://dash.cloudflare.com -> Add a
domain -> bluemet.org -> Free plan. Cloudflare shows two nameservers.
At your registrar (wherever bluemet.org was bought) replace the
nameservers with those two. This does NOT change where the site is
served yet: Cloudflare imports the existing records, which still
point at Render. Wait until the Cloudflare dashboard says Active
(minutes to a few hours).

6b. Zero Trust -> Networks -> Tunnels -> Create a tunnel -> Cloudflared
-> name it `bluemet-mac`. On the next screen pick macOS: it shows one
command like

    sudo cloudflared service install eyJhIjoi...

Run it on the Mac. That installs cloudflared as a system service that
starts at boot and reconnects on its own.

6c. Same screen, Public Hostname -> Add:
- Subdomain: `mac`   Domain: `bluemet.org`
- Service: HTTP, URL `localhost:8501`
Save.

Check: from your phone on cellular, open https://mac.bluemet.org. You
should see the login page. (Streamlit needs WebSockets; Cloudflare has
them on by default. If the page loads but never finishes, check Zero
Trust -> Networks -> Tunnels -> bluemet-mac shows HEALTHY.)

## 7. Make Streamlit a service

    sed "s/YOURNAME/$(whoami)/g" mac/org.bluemet.streamlit.plist > /tmp/org.bluemet.streamlit.plist
    sudo cp /tmp/org.bluemet.streamlit.plist /Library/LaunchDaemons/
    sudo chown root:wheel /Library/LaunchDaemons/org.bluemet.streamlit.plist
    sudo launchctl bootstrap system /Library/LaunchDaemons/org.bluemet.streamlit.plist

Check: `tail -f ~/bluemet/streamlit.log` shows Streamlit starting;
https://mac.bluemet.org works. Reboot the Mac and check again: both
services come back on their own.

Useful later:

    sudo launchctl kickstart -k system/org.bluemet.streamlit   # restart
    sudo launchctl bootout system/org.bluemet.streamlit         # stop

## 8. Cut over bluemet.org

Only now. In the tunnel's Public Hostname tab add a second hostname:
Subdomain blank, Domain bluemet.org, Service HTTP localhost:8501.
Cloudflare replaces the old record that pointed at Render. Add `www`
the same way if you use it.

Check https://bluemet.org from cellular. Keep Render running for a
week as a fallback (switch the hostname back in the tunnel panel if
the Mac has a problem), then suspend the Render service.

## 9. Updating the site

Automatic (recommended): the auto-pull job checks GitHub every 3
minutes and, when `main` has moved, pulls and restarts Streamlit.
Install once:

    sed "s/YOURNAME/$(whoami)/g" mac/org.bluemet.autopull.plist > /tmp/org.bluemet.autopull.plist
    sudo cp /tmp/org.bluemet.autopull.plist /Library/LaunchDaemons/
    sudo chown root:wheel /Library/LaunchDaemons/org.bluemet.autopull.plist
    sudo launchctl bootstrap system /Library/LaunchDaemons/org.bluemet.autopull.plist

Check: `tail ~/bluemet/autopull.log` after a push shows
"updated ... streamlit restarted". It only fast-forwards: if the Mac
has local edits or commits that are not on GitHub it logs that and
leaves the checkout alone until you sort it out with `git status`.

By hand:

    cd ~/bluemet/wx_compare && git pull
    sudo launchctl kickstart -k system/org.bluemet.streamlit

## What to expect

- Restarts: a crash restarts within 10 s (KeepAlive). Power failure:
  the Mac boots, logs in, both services start, no keyboard needed.
- Outage risk you now own: home power, home internet, and anyone who
  unplugs the Mac. A UPS covers the first for short outages.
- Bandwidth: the site pushes PNG/WebP maps; a home upload of 20 Mbps
  or better is comfortable for a handful of desks.


## 10. Going Mac-only (5 Oct 2026)

Render is not part of deploying: pushing to `main` is the deploy (step
9). Render only answered bluemet.org. To retire it:

1. `mac/.env`: uncomment `TOMORROWIO_API_KEY=` (done 5 Oct). The next
   auto-pull restart picks it up; `grep tomorrow static/tio_warmer.log`
   should stop saying "no TOMORROWIO_API_KEY".
2. Cloudflare Zero Trust -> Tunnels -> the BlueMet tunnel -> Public
   Hostnames -> add `bluemet.org` and `www.bluemet.org`, service
   `http://127.0.0.1:8501` (same as mac.bluemet.org). Let Cloudflare
   replace the DNS records that pointed at Render.
3. Test https://bluemet.org (login, Weather Mapping tiles, Convection
   Parameters), then delete the Render service. The `/opt/render`
   paths in the code are unused fallbacks on the Mac.

The Mac is then the only instance and the only holder of the
tomorrow.io key. Energy Saver: never sleep, start after power failure.
