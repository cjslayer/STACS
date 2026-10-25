# STACS
Next: Run SLAC/stac/stac_cli.py ping, tables, collect and monitor against the bench CR6 over serial and save the live captures to tests/fixtures.

- [x] CR6/LoggerNet CSV and TOA5 data analyzer GUI with chop, stats, export
- [x] Package analyzer as standalone offline Windows exe via PyInstaller
- [x] VW Live Monitor for two vibrating-wire load cells in the analyzer
- [x] Camera motion trigger that auto-fills Time Chop start and end
- [x] STAC PakBus/BMP5 protocol library and CLI with 125 offline tests
- [x] DicLab headless muDIC core and Basler/OpenCV capture backends tested
- [x] DIC camera validation: noise floor, rail alignment, displacement trials
- [ ] Validate STAC on bench CR6 hardware and record live packet fixtures
- [ ] Smoke-test DicLab GUI on the lab Windows PC
- [ ] Validate DIC to about 1% using a gauge block or dial indicator reference
- [ ] Add pixel-to-mm calibration and speckle quality checker to DicLab
- [ ] Integrate STAC live collection and DIC into lab test workflow and deploy
