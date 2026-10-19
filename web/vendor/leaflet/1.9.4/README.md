Leaflet 1.9.4 is vendored locally under the BSD 2-Clause license in `LICENSE`.
`provenance.json` records the official release revision, download sources, byte sizes
and SHA-256 values. JS/CSS match the SRI hashes on the [official download page](https://leafletjs.com/download.html).
They are served locally, with no CDN dependency during use.

The street layer uses the [OSM standard tile service](https://operations.osmfoundation.org/policies/tiles/)
for normal interactive viewing, with visible attribution and an origin-only cross-site
Referer. The browser honours tile HTTP cache headers. The service worker does not cache
external tiles; there is no bulk download, prefetch, offline map feature or automatic
retry loop. Service availability is best effort. POI and route overlays are local;
straight route segments are not road routing or walking directions.
