{ config, lib, pkgs, ... }:
let
  cfg = config.programs.forcaslicer-mcp;
  package = import ./package.nix { inherit pkgs; };
in {
  options.programs.forcaslicer-mcp = {
    enable = lib.mkEnableOption "FOrcaSlicer MCP background tools";
    package = lib.mkOption { type = lib.types.package; default = package; description = "MCP package; use the flake package for its fully locked runtime"; };
    workspace = lib.mkOption { type = lib.types.str; default = "${config.home.homeDirectory}/Documents/FOrcaSlicer"; description = "Allowed project and output directory"; };
    printerUrl = lib.mkOption { type = lib.types.str; default = ""; description = "Direct Moonraker HTTP(S) origin"; };
    apiKeyFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; description = "Optional private file read at runtime; never put a key in the Nix store"; };
  };
  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];
    xdg.configFile."forcaslicer-mcp/config.json".text = builtins.toJSON {
      inherit (cfg) workspace;
      printer_url = cfg.printerUrl;
      api_key_file = cfg.apiKeyFile;
    };
  };
}
