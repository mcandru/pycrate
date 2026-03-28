import tarfile

# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------


def extract_image(image_path: str, target_dir: str) -> None:
    """
    "Pull" an image, in our case, just extract a tarball.

    A real container runtime like Docker uses a layered filesystem (overlayfs)
    where each layer in the image is stacked on top of the previous one.
    For simplicity, we just extract a flat tarball. The effect is the same:
    the container gets a root filesystem populated with the image contents.
    """
    print(f"Extracting image: {image_path}")
    with tarfile.open(image_path) as tar:
        tar.extractall(path=target_dir)
    print(f"Image extracted to: {target_dir}")
