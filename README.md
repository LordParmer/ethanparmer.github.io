# ethanparmer.github.io

## Post art

Each post gets its own artwork, used for its list thumbnail, the image at the
top of the post and its share image (og:image). After adding a post, or
changing its tags or `art` settings, run:

```sh
python tools/post_art.py
```

and commit the PNG it writes to `assets/images/art/`. The deploy workflow runs
the same script before building, so a post pushed without its art still gets
it. Options, colours per tag and front matter overrides are described at the
top of `tools/post_art.py`.
