{ config, lib, pkgs, ... }:

let
  cfg = config.services.ausweis-sak-advertise;
in
{
  options.services.ausweis-sak-advertise = {
    enable = lib.mkEnableOption "advertise a paired AusweisApp smartphone as a remote card reader";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      description = "The ausweis-sak-advertise package to run.";
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Path to the AusweisApp config file.  Defaults to
        ~/.config/AusweisApp/AusweisApp2.conf when null.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra arguments passed to ausweis-sak-advertise.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.ausweis-sak-advertise = {
      Unit = {
        Description = "Advertise smartphone as AusweisApp card reader";
      };

      Service = {
        ExecStart = lib.concatStringsSep " " (
          [
            "${cfg.package}/bin/ausweis-sak-advertise"
            "--adb ${pkgs.android-tools}/bin/adb"
            "--ip ${pkgs.iproute2}/bin/ip"
          ]
          ++ lib.optional (cfg.configFile != null) "--config ${cfg.configFile}"
          ++ cfg.extraArgs
        );
        Restart = "on-failure";
        RestartSec = "10";
      };

      wantedBy = [ "default.target" ];
    };
  };
}
