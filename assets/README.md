# Repository artwork

- `banner.svg` preserves the original compact banner.
- `social-preview.svg` is the editable source for the link-sharing card.
- `social-preview.png` is the opaque 1280 × 640 export used as the README banner
  and uploaded in **Repository Settings → General → Social preview**.

The card shares the banner's colors and pipeline/checkmark motif. Its text states
the skill's purpose without presenting target timings as measured results.

To reproduce the PNG with [CairoSVG](https://cairosvg.org/), run from the repository
root in an environment with CairoSVG available:

```sh
cairosvg assets/social-preview.svg -o assets/social-preview.png
```

CairoSVG is an artwork tool, not a dependency of the skill or its CI. Keep the
export below 1 MB and upload it again after changing the artwork. Committing an
image or adding metadata to the README does not update GitHub's Social preview
setting. See [GitHub's social preview documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).
