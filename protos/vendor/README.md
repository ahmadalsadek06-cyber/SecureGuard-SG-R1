# Bundled Webots R2025a assets

These assets are from the pinned Cyberbotics Webots **R2025a** release:
https://github.com/cyberbotics/webots/tree/R2025a

Original copyright and license notices are retained in each PROTO. These assets
are licensed for use with Webots: https://cyberbotics.com/webots_assets_license

The generator rewrites resource URLs to relative local paths and bundles all
referenced robot textures and PROTOs, including the inactive E-puck 2 textures.
The E-puck PROTO's default `window` is changed to `<generic>` to use Webots' built-in
inspector and avoid optional E-puck GUI / Bluetooth DLL dependencies. No robot
physics, geometry, motors, sensors, or controller behavior is changed. Webots'
standard JavaScript template modules are provided by the Webots installation.

Keep this folder together with the project. Do not use these assets outside Webots.

For repository publication, EXIF and related metadata were removed from four JPEG
textures without recompressing them. Their decoded pixels and appearance are unchanged.
