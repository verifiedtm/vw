{
  description = "Development shell for vw";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python313;
      in
      {
        formatter = pkgs.nixfmt;

        devShells.default = pkgs.mkShell {
          packages = [
            python

            # Python tools
            pkgs.uv
            pkgs.ruff

            # Nix tools
            pkgs.nixfmt
            pkgs.statix
            pkgs.deadnix

            # Other tools
            pkgs.just
            pkgs.fd
            pkgs.ffmpeg
          ];

          # Pillow's manylinux wheel links to the system zlib.
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.zlib ];
          # Keep uv on the interpreter supplied by Nix.
          UV_PYTHON = "${python}/bin/python";
          UV_PYTHON_DOWNLOADS = "never";
        };
      }
    );
}
