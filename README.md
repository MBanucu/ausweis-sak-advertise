# ausweis-sak-advertise

Keep a **smartphone card reader** for [AusweisApp](https://www.ausweisapp.bund.de/)
discoverable when the phone is running as a **Wi-Fi hotspot** (or USB tethering).

## Problem

AusweisApp pairs a PC with a smartphone and then uses the phone's NFC to read the
German ID card (eID). Discovery happens via UDP broadcast (`REMOTE_IFD` on port
24727).

When the phone acts as a Wi-Fi hotspot, Android routes the app's UDP broadcast out
the cellular/default network instead of the hotspot interface (`ap0`), so the PC
never sees it and can't connect. The phone shows "Card reader ready", but the PC
shows the device as unavailable.

## What this does

A small service on the PC advertises the phone by feeding a forged `REMOTE_IFD`
datagram to the local AusweisApp. It discovers the phone in one of two ways:

1. **adb** (preferred): reads the phone's IP, model, and AusweisApp TCP port over
   USB debugging.
2. **network** (fallback when adb has no device): assumes the phone is the default
   gateway (true for hotspot/tethering) and port-scans its ephemeral range.

It then auto-detects the phone's state by probing its TLS server and advertises
accordingly:

- **card-reader mode** (paired): advertises the real certificate fingerprint with
  `pairing: false`;
- **pairing mode**: advertises a fresh IFDID with `pairing: true`, so the phone
  shows up in the PC's "Add pairing" list.

So a single service covers both initial pairing and everyday use. No root is
required on the phone.

## Prerequisites

- AusweisApp 2.x installed on both PC and phone (matching versions).
- The phone **paired once** with the PC (Settings → "Smartphone as card reader").
  Pairing is a manual step and is not automated by this project.
- The phone's **"Card reader"** mode active.
- For the adb strategy: USB debugging enabled and the phone authorized.
- For the network strategy: the phone is the PC's default gateway (hotspot or
  USB tethering).

## Usage

### NixOS

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    ausweis-sak-advertise.url = "github:MBanucu/ausweis-sak-advertise";
  };

  outputs = { nixpkgs, ausweis-sak-advertise, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        ausweis-sak-advertise.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Then, in `configuration.nix`:

```nix
{ services.ausweis-sak-advertise.enable = true; }
```

### Home Manager

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager.url = "github:nix-community/home-manager";
    ausweis-sak-advertise.url = "github:MBanucu/ausweis-sak-advertise";
  };

  outputs = { home-manager, ausweis-sak-advertise, ... }: {
    homeConfigurations.michi = home-manager.lib.homeManagerConfiguration {
      modules = [
        ausweis-sak-advertise.homeManagerModules.default
      ];
    };
  };
}
```

Then, in your home-manager config:

```nix
{ services.ausweis-sak-advertise.enable = true; }
```

### Standalone

```sh
nix run github:MBanucu/ausweis-sak-advertise -- --help
```

## Options

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `services.ausweis-sak-advertise.enable` | bool | `false` | Enable the service. |
| `...package` | package | `pkgs.callPackage ./package.nix {}` | The package to run. |
| `...configFile` | null or path | `null` | Override the AusweisApp config path (defaults to `~/.config/AusweisApp/AusweisApp2.conf`). |
| `...name` | null or str | `null` | Device name to advertise (default: auto-detected). |
| `...extraArgs` | list of str | `[]` | Extra CLI args, e.g. `[ "--no-adb" ]`. |

## Pairing a device

Pairing must happen once before the reader can be used. The service already
advertises in pairing mode automatically, so just start it (or leave the systemd
service running) and:

1. On the phone, open **Card reader → Pair new device** (a 4-digit code is shown).
2. On the PC, open **Settings → Smartphone as card reader** and press
   **Pair device**.
3. Enter the code shown on the phone. The code rotates every two minutes.

If you need to force pairing mode, run with `--pairing`.

## CLI flags

```
--config PATH      AusweisApp config file
--adb PATH         path to adb (default: adb in PATH)
--ip PATH          path to ip (default: ip in PATH)
--no-adb           always use the network strategy
--pairing          force pairing mode (default: auto-detect)
--name NAME        device name to advertise (default: auto-detected)
--port-range LO HI ephemeral port range to scan (default: 32768 60999)
--udp-port PORT    local UDP port (default: 24727)
```

## Limitations

- Pairing is manual and must be done before the service can work.
- The network fallback assumes the phone is the default gateway. If the phone and
  PC are both clients of a normal Wi-Fi network, AusweisApp's own broadcast works
  and this service is unnecessary.
- The network fallback port-scans the phone (32768–60999); the result is cached
  and only rescanned when the port changes (e.g. the app restarts).
- The adb strategy requires USB debugging to remain enabled.

## License

MIT — see [LICENSE](LICENSE).
