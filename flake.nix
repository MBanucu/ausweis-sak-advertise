{
  description = "Keep an AusweisApp smartphone card reader discoverable over a Wi-Fi hotspot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.callPackage ./package.nix { };
          ausweis-sak-advertise = pkgs.callPackage ./package.nix { };
        }
      );

      nixosModules.default = ./module.nix;

      homeManagerModules.default = ./module.nix;

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python3
              android-tools
              iproute2
            ];
          };
        }
      );
    };
}
