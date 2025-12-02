---
title: "CDC/RDC Constraint and Waiver Files"
date: "2025-12-04"
---

Many Caliptra integrators have asked about acquiring the configuration files
used to sign off on CDC, RDC, and Lint flows for Caliptra RTL. The consortium is
unfortunately unable to publish these files as open source due to the use of
proprietary EDA tools and associated licensing requirements. Refer to GitHub
issues: https://github.com/chipsalliance/caliptra-rtl/issues/532 and
https://github.com/chipsalliance/caliptra-ss/issues/859.

Consortium representatives can provide these files on a case-by-case basis to
integrators upon request. To facilitate this request, integrators must establish
a non-disclosure agreement (NDA) with each of the consortium representative
companies that provide these files.

Specifically:

- Lint (Synopsys Spyglass): Lint policy and waiver files are maintained by
  Microsoft
- Clock Domain Crossing (Questa CDC): CDC constraints are maintained by AMD
- Reset Domain Crossing (Real Intent Meridian): RDC constraints are maintained
  by NVIDIA

Upon establishing an NDA with the above companies, integrators may request the
configuration files by contacting the consortium directly via email at
[caliptra-wg+owner@lists.chipsalliance.org](mailto:caliptra-wg+owner@lists.chipsalliance.org).
A consortium representative will respond with direct points of contact to reach
each company for the materials.
