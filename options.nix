{ lib, pkgs, ... }:

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

    name = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Device name to advertise (default: auto-detected).";
    };

    requireProcess = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Only advertise while a local process with this name is running
        (e.g. "AusweisApp").  When null, advertise unconditionally.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra arguments passed to ausweis-sak-advertise.";
    };
  };
}
