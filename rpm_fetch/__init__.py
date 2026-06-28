"""rpm-fetch: warm an Artifactory cache from an RPM repository's metadata.

Reads a yum/dnf repository's ``repodata`` (repomd.xml -> primary metadata),
enumerates every package's ``<location href>``, and issues concurrent HEAD (or
GET) requests against the corresponding Artifactory paths to populate the cache.

Pure standard library; no third-party dependencies.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
