The Mac native package includes an unmodified FFmpeg binary from the pinned
imageio-ffmpeg 0.6.0 distribution, used as a separate process for live audio.
FFmpeg and the enabled GPL components are distributed under GPLv3; see
`ffmpeg-COPYING.GPLv3`. The application calls it over local RTSP, without linking.

Binary distributions and build/source information:
- https://pypi.org/project/imageio-ffmpeg/0.6.0/
- https://github.com/imageio/imageio-binaries/tree/master/ffmpeg
- https://ffmpeg.org/download.html

Run the bundled executable with `-version` and `-L` to inspect its build options
and notices. Its SHA-256 is recorded in bundle.json. No user video is sent to
these projects. Builds must preserve the upstream source and licensing notices
when redistributing this component.
