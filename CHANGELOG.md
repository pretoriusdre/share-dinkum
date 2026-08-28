# Changelog

All notable changes to Share Dinkum are recorded here. 

To upgrade, stop the server and run `uv run update`.

## 0.2.0

Version 0.2.0 is the start of formal version tagging, although the tool has been under development for quite some time prior to this point.

### Added

- The dashboard shows the installed version and tells you when a newer release is available.
- `uv run dev make_import_template` generates an empty Excel import template from the models, so a
  new portfolio no longer starts by deleting sample rows out of a copy of the sample file.
- The import notebook takes a list of portfolios, so several Excel files can be loaded, each into its
  own portfolio.

### Changed

- A file is now loaded as a single transaction. A file that fails part way through, most often
  because a transaction names an instrument that was never listed, leaves nothing behind instead of a
  half loaded portfolio.
- Loading the same file again updates rows rather than duplicating them, matching on `legacy_id` for
  transactions and on the unique fields of reference tables such as Market and Instrument. Rows with
  no `legacy_id` are still always added, since two identical buys on one day are two real buys.
- A portfolio name must be unique per owner, as files are matched to portfolios by name.
- `clear_all_data` has moved to the bottom of the import notebook, commented out, under **Danger
  zone**. It deletes every portfolio and was never part of importing, though the README implied it
  was.

### Fixed

- Loading a data export into a *different* portfolio silently moved its records out of the original
  one instead of copying them. This is now refused.
- Updating an existing record during a load saved it twice, running every signal twice.


Planning is ongoing for implementation of the 2027 Austrlaian Capital Gains Tax changes.