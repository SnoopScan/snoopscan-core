"""Places — business listings as a source (docs/internal/12-places-source.md).

The front of a pipeline that already has a back: `engine/leadgen` turns a
company website into verified contacts; Places is where the companies come
from. v0 reads the rendered Google Maps results page — names, place links
(which carry the feature id and coordinates), rating, category, address —
and, per place, the detail panel for website and phone. The internal
`tbm=map` endpoint and its `pb` encoding are a later, separate piece of work.

Proprietary: depends on the browser tiers, the proxy layer and leadgen.
"""
