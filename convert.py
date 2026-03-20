import argparse

import genie_tts as genie


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Genie TTS weights to ONNX")
    parser.add_argument("torch_pth_path", help="Path to the SoVITS .pth weights file")
    parser.add_argument("torch_ckpt_path", help="Path to the GPT .ckpt weights file")
    parser.add_argument("output_dir", help="Directory to write the ONNX output into")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    genie.convert_to_onnx(
        torch_pth_path=args.torch_pth_path,
        torch_ckpt_path=args.torch_ckpt_path,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()