{
  description = "FOrcaSlicer background slicing and Snapmaker U1 MCP tools";
  inputs.nixpkgs.url = "https://channels.nixos.org/nixos-26.05/nixexprs.tar.xz";
  outputs = { self, nixpkgs }:
    let system = "x86_64-linux"; pkgs = import nixpkgs { inherit system; }; package = import ./nix/package.nix { inherit pkgs; };
    in {
      packages.${system} = { default = package; forcaslicer-mcp = package; };
      apps.${system}.default = { type = "app"; program = "${package}/bin/forcaslicer-mcp"; };
      homeManagerModules.default = import ./nix/home-manager.nix;
      checks.${system}.unit = pkgs.runCommand "forcaslicer-mcp-unit" { nativeBuildInputs = [ package.python ]; } ''
        cp ${./server.py} server.py
        cp ${./gcode_audit.py} gcode_audit.py
        cp -r ${./tests} tests
        export TEST_STATE_DIRECTORY="$PWD/test-state"
        python -m unittest discover -s tests -v
        touch $out
      '';
    };
}
