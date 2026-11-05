{ config, lib, pkgs, ... }:

let
  cfg = config.services.ausweis-sak-advertise;
in
{
  imports = [ ./options.nix ];

  config = lib.mkIf cfg.enable {
    systemd.user.services.ausweis-sak-advertise = {
      description = "Advertise smartphone as AusweisApp card reader";
      wantedBy = [ "default.target" ];

      serviceConfig = {
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
    };
  };
}
